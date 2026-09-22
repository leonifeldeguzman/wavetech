from flask import abort, flash, redirect, render_template, request, session, url_for

from app.blueprints.announcements import announcements_bp
from app.extensions import db
from app.models.announcement import Announcement
from app.services import activity_log_service, announcement_service
from app.services.announcement_service import AnnouncementValidationError
from app.utils.api_responses import success_response
from app.utils.decorators import login_required


def _log(action, details=None):
    """Record one Activity Log entry for an announcement action, using the
    same admin-name/user-id convention as the Settings blueprint (see
    app/blueprints/settings/routes.py)."""
    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=action,
        details=details,
    )


# ---------------------------------------------------------------------------
# Announcement Management (shared Admin/Operator access)
#
# Admin and Operator accounts have identical Announcement/Safety Alert
# management access. Every route below that creates, publishes, schedules,
# edits, activates, deactivates/cancels, or deletes an announcement only
# requires an authenticated session (see app/utils/decorators.py) — the
# same as the full Announcement Management view in `index()` below.
# ---------------------------------------------------------------------------


@announcements_bp.route("/announcements")
@login_required
def index():
    # Requirement 9: no scheduler/cron — a Scheduled announcement becomes
    # Active the next time this (or the passenger-facing) page is loaded.
    announcement_service.publish_due_announcements()

    # Active announcements — the "Published Announcements" section below.
    published = announcement_service.get_published_announcements()

    scheduled = announcement_service.get_scheduled_announcements()
    inactive = announcement_service.get_inactive_announcements()

    # Optional prefill coming from the Scheduling Decision-Support "Create
    # Safety Alert" reference link on the dashboard (requirement 7). Never
    # persisted here — it only pre-fills the Create Announcement form so an
    # admin can review/edit before submitting.
    prefill = {
        "title": request.args.get("prefill_title", ""),
        "content": request.args.get("prefill_content", ""),
        "type": request.args.get("prefill_type", Announcement.TYPE_ANNOUNCEMENT),
    }

    return render_template(
        "announcements/index.html",
        published=published,
        scheduled=scheduled,
        inactive=inactive,
        prefill=prefill,
        types=[Announcement.TYPE_ANNOUNCEMENT, Announcement.TYPE_SAFETY_ALERT],
        active_page="announcements",
    )


@announcements_bp.route("/announcements/create", methods=["POST"])
@login_required
def create():
    form = request.form
    action = form.get("action")  # "publish_now" or "schedule"
    publish_now = action == "publish_now"

    try:
        announcement = announcement_service.create_announcement(
            title=form.get("title"),
            content=form.get("content"),
            type_=form.get("type"),
            publish_now=publish_now,
            scheduled_at_raw=form.get("scheduled_at"),
            created_by_user_id=session.get("user_id"),
        )
    except AnnouncementValidationError as exc:
        flash(str(exc))
        return redirect(url_for("announcements.index"))

    if publish_now:
        _log(
            activity_log_service.ACTION_ANNOUNCEMENT_PUBLISHED,
            details=f"'{announcement.title}' ({announcement.type})",
        )
        flash("Announcement published successfully.")
    else:
        _log(
            activity_log_service.ACTION_ANNOUNCEMENT_SCHEDULED,
            details=(
                f"'{announcement.title}' ({announcement.type}) "
                f"scheduled for {announcement.scheduled_at}"
            ),
        )
        flash("Announcement scheduled successfully.")
    return redirect(url_for("announcements.index"))


def _get_announcement_or_404(announcement_id):
    announcement = db.session.get(Announcement, announcement_id)
    if announcement is None:
        abort(404)
    return announcement


@announcements_bp.route("/announcements/<int:announcement_id>/edit", methods=["POST"])
@login_required
def edit(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    form = request.form

    try:
        announcement_service.update_announcement(
            announcement,
            title=form.get("title"),
            content=form.get("content"),
            type_=form.get("type"),
            scheduled_at_raw=form.get("scheduled_at"),
        )
    except AnnouncementValidationError as exc:
        flash(str(exc))
        return redirect(url_for("announcements.index"))

    _log(
        activity_log_service.ACTION_ANNOUNCEMENT_EDITED,
        details=f"'{announcement.title}' ({announcement.type})",
    )
    flash("Announcement updated successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/delete", methods=["POST"])
@login_required
def delete(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    title, type_ = announcement.title, announcement.type
    announcement_service.delete_announcement(announcement)

    _log(
        activity_log_service.ACTION_ANNOUNCEMENT_DELETED,
        details=f"'{title}' ({type_})",
    )
    flash("Announcement deleted successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/activate", methods=["POST"])
@login_required
def activate(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    announcement_service.activate_announcement(announcement)

    _log(
        activity_log_service.ACTION_ANNOUNCEMENT_ACTIVATED,
        details=f"'{announcement.title}' ({announcement.type})",
    )
    flash("Announcement activated successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/deactivate", methods=["POST"])
@login_required
def deactivate(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    # A still-Scheduled announcement (never actually published) is being
    # cancelled; an already-Published one is being deactivated. Same
    # service call either way — see announcement_service.deactivate_announcement.
    was_never_published = announcement.published_at is None
    announcement_service.deactivate_announcement(announcement)

    if was_never_published:
        _log(
            activity_log_service.ACTION_ANNOUNCEMENT_CANCELLED,
            details=f"'{announcement.title}' ({announcement.type})",
        )
        flash("Scheduled announcement cancelled successfully.")
    else:
        _log(
            activity_log_service.ACTION_ANNOUNCEMENT_DEACTIVATED,
            details=f"'{announcement.title}' ({announcement.type})",
        )
        flash("Announcement deactivated successfully.")
    return redirect(url_for("announcements.index"))


# ---------------------------------------------------------------------------
# Passenger-facing display
#
# Public, like the rest of the Passenger Portal (app/blueprints/passenger)
# — no login_required, since passengers have no account/session here.
# ---------------------------------------------------------------------------


@announcements_bp.route("/passenger/announcements")
def passenger_announcements():
    announcements = announcement_service.get_active_announcements_for_passengers()
    return render_template(
        "passenger/announcements.html",
        announcements=announcements,
        active_page="announcements",
    )


def _serialize_announcement(announcement):
    return {
        "id": announcement.id,
        "title": announcement.title,
        "content": announcement.content,
        "type": announcement.type,
        "published_at": (
            announcement.published_at.isoformat() if announcement.published_at else None
        ),
    }


@announcements_bp.route("/api/announcements", methods=["GET"])
def api_list_announcements():
    announcements = announcement_service.get_active_announcements_for_passengers()
    return success_response(data=[_serialize_announcement(a) for a in announcements])