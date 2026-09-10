from flask import abort, flash, redirect, render_template, request, session, url_for

from app.blueprints.announcements import announcements_bp
from app.extensions import db
from app.models.announcement import Announcement
from app.services import announcement_service
from app.services.announcement_service import AnnouncementValidationError
from app.utils.api_responses import success_response
from app.utils.decorators import admin_required, login_required

# ---------------------------------------------------------------------------
# Admin: Announcement Management
#
# Like the Manifests blueprint (app/blueprints/manifests/routes.py), this
# uses login_required rather than admin_required: any authenticated staff
# member (Admin or Operator) can manage announcements, the same way any
# logged-in staff member manages trips/manifests. It's only the stricter
# Admin-only Settings (Safety Thresholds, Security & Activity) that use
# admin_required elsewhere in this project.
# ---------------------------------------------------------------------------


@announcements_bp.route("/announcements")
@login_required
def index():
    # Requirement 9: no scheduler/cron — a Scheduled announcement becomes
    # Active the next time this (or the passenger-facing) page is loaded.
    announcement_service.publish_due_announcements()

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
@admin_required
def create():
    form = request.form
    action = form.get("action")  # "publish_now" or "schedule"

    try:
        announcement_service.create_announcement(
            title=form.get("title"),
            content=form.get("content"),
            type_=form.get("type"),
            publish_now=(action == "publish_now"),
            scheduled_at_raw=form.get("scheduled_at"),
            created_by_user_id=session.get("user_id"),
        )
    except AnnouncementValidationError as exc:
        flash(str(exc))
        return redirect(url_for("announcements.index"))

    if action == "publish_now":
        flash("Announcement published successfully.")
    else:
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

    flash("Announcement updated successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/delete", methods=["POST"])
@login_required
def delete(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    announcement_service.delete_announcement(announcement)
    flash("Announcement deleted successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/activate", methods=["POST"])
@login_required
def activate(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    announcement_service.activate_announcement(announcement)
    flash("Announcement activated successfully.")
    return redirect(url_for("announcements.index"))


@announcements_bp.route("/announcements/<int:announcement_id>/deactivate", methods=["POST"])
@login_required
def deactivate(announcement_id):
    announcement = _get_announcement_or_404(announcement_id)
    announcement_service.deactivate_announcement(announcement)
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