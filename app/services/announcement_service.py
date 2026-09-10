"""Safety Alerts / Announcement Management — business logic.

This is the single place Announcement rows are created/validated/updated,
mirroring how app/services/settings_service.py centralizes Admin Settings
and app/services/activity_log_service.py centralizes audit logging.

Automatic scheduled publishing (task requirement 9): this project has no
background scheduler/cron/Celery/Redis. `publish_due_announcements()` is
the "simplest approach compatible with the existing project" — it is
called at the top of the admin Announcement Management page and the
passenger-facing announcements page (see app/blueprints/announcements/
routes.py), so any announcement whose scheduled time has passed is
activated the next time either page is loaded, with no manual publish
step required.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.models.announcement import Announcement
from app.utils.timezone import utc_now_naive, PH_TZ

VALID_TYPES = {Announcement.TYPE_ANNOUNCEMENT, Announcement.TYPE_SAFETY_ALERT}
VALID_STATUSES = {Announcement.STATUS_ACTIVE, Announcement.STATUS_INACTIVE}

SCHEDULE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M"  # matches <input type="datetime-local">

TITLE_MAX_LENGTH = 200


class AnnouncementValidationError(ValueError):
    """Raised when create/update input fails validation."""


# ---------------------------------------------------------------------------
# Field validation helpers
# ---------------------------------------------------------------------------

def _clean_title(raw) -> str:
    title = (raw or "").strip()
    if not title:
        raise AnnouncementValidationError("Title is required.")
    if len(title) > TITLE_MAX_LENGTH:
        raise AnnouncementValidationError(
            f"Title must be {TITLE_MAX_LENGTH} characters or fewer."
        )
    return title


def _clean_content(raw) -> str:
    content = (raw or "").strip()
    if not content:
        raise AnnouncementValidationError("Message is required.")
    return content


def _clean_type(raw) -> str:
    value = (raw or "").strip()
    if value not in VALID_TYPES:
        raise AnnouncementValidationError(
            "Type must be one of: " + ", ".join(sorted(VALID_TYPES)) + "."
        )
    return value


def _parse_schedule_datetime(raw) -> datetime:
    raw = (raw or "").strip()

    if not raw:
        raise AnnouncementValidationError(
            "A scheduled date/time is required."
        )

    try:
        # datetime-local input is entered in Philippine Time.
        ph_datetime = datetime.strptime(
            raw,
            SCHEDULE_DATETIME_FORMAT
        )
    except ValueError:
        raise AnnouncementValidationError(
            "Scheduled date/time is invalid."
        )

    # Treat the admin's input as Asia/Manila time.
    ph_datetime = ph_datetime.replace(tzinfo=PH_TZ)

    # Convert PHT → UTC, then remove tzinfo for DB storage.
    scheduled_at = (
        ph_datetime
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

    if scheduled_at <= utc_now_naive():
        raise AnnouncementValidationError(
            "Scheduled date/time must be in the future."
        )

    return scheduled_at


# ---------------------------------------------------------------------------
# Automatic scheduled publishing
# ---------------------------------------------------------------------------

def publish_due_announcements() -> int:
    """Activate any Scheduled announcement whose time has arrived.

    Safe to call on every request that touches announcements — it's a
    cheap, idempotent query. Returns the number of announcements that were
    just published, mostly useful for tests.
    """
    now = utc_now_naive()
    due = Announcement.query.filter(
        Announcement.status == Announcement.STATUS_INACTIVE,
        Announcement.scheduled_at.isnot(None),
        Announcement.scheduled_at <= now,
        Announcement.published_at.is_(None),
    ).all()

    for announcement in due:
        announcement.status = Announcement.STATUS_ACTIVE
        announcement.published_at = now

    if due:
        db.session.commit()

    return len(due)


# ---------------------------------------------------------------------------
# Create / Publish Now / Schedule
# ---------------------------------------------------------------------------

def create_announcement(
    *,
    title,
    content,
    type_,
    publish_now: bool,
    scheduled_at_raw=None,
    created_by_user_id=None,
) -> Announcement:
    """Validate and create a new Announcement.

    Raises AnnouncementValidationError on any invalid input; nothing is
    written to the database in that case.
    """
    clean_title = _clean_title(title)
    clean_content = _clean_content(content)
    clean_type = _clean_type(type_)

    announcement = Announcement(
        title=clean_title,
        content=clean_content,
        type=clean_type,
        created_by_user_id=created_by_user_id,
    )

    if publish_now:
        announcement.status = Announcement.STATUS_ACTIVE
        announcement.published_at = utc_now_naive()
        announcement.scheduled_at = None
    else:
        announcement.scheduled_at = _parse_schedule_datetime(scheduled_at_raw)
        announcement.status = Announcement.STATUS_INACTIVE
        announcement.published_at = None

    db.session.add(announcement)
    db.session.commit()
    return announcement


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def update_announcement(
    announcement: Announcement,
    *,
    title,
    content,
    type_,
    scheduled_at_raw=None,
) -> Announcement:
    """Update title/content/type. If the announcement hasn't published yet
    (still Scheduled) and a new scheduled_at_raw is supplied, it is
    re-validated and updated too — this is how an admin edits a Scheduled
    announcement's date/time. Already-published announcements keep their
    original scheduled_at/published_at as history."""
    announcement.title = _clean_title(title)
    announcement.content = _clean_content(content)
    announcement.type = _clean_type(type_)

    if announcement.published_at is None and scheduled_at_raw:
        announcement.scheduled_at = _parse_schedule_datetime(scheduled_at_raw)

    db.session.commit()
    return announcement


# ---------------------------------------------------------------------------
# Activate / Deactivate / Delete
# ---------------------------------------------------------------------------

def deactivate_announcement(announcement: Announcement) -> Announcement:
    """Deactivate a Published announcement, or cancel a still-Scheduled one.
    Either way this just flips status to Inactive; scheduled_at/
    published_at are left untouched so the row's history is preserved."""
    announcement.status = Announcement.STATUS_INACTIVE
    db.session.commit()
    return announcement


def activate_announcement(announcement: Announcement) -> Announcement:
    """Manually (re)activate an announcement. If it was never actually
    published (e.g. a scheduled one that was cancelled before its time),
    stamp published_at now."""
    announcement.status = Announcement.STATUS_ACTIVE
    if announcement.published_at is None:
        announcement.published_at = utc_now_naive()
    db.session.commit()
    return announcement


def delete_announcement(announcement: Announcement) -> None:
    db.session.delete(announcement)
    db.session.commit()


# ---------------------------------------------------------------------------
# Queries used by the admin management page and the passenger-facing page
# ---------------------------------------------------------------------------

def get_published_announcements():
    """Currently Active/live announcements, most recently published first."""
    return (
        Announcement.query.filter(
            Announcement.status == Announcement.STATUS_ACTIVE
        )
        .order_by(Announcement.published_at.desc())
        .all()
    )


def get_scheduled_announcements():
    """Announcements waiting for their scheduled time (not yet published)."""
    return (
        Announcement.query.filter(
            Announcement.status == Announcement.STATUS_INACTIVE,
            Announcement.scheduled_at.isnot(None),
            Announcement.published_at.is_(None),
        )
        .order_by(Announcement.scheduled_at.asc())
        .all()
    )


def get_inactive_announcements():
    """Previously-published announcements an admin has since deactivated
    (i.e. Inactive, but with published_at already set). Kept separate from
    "Scheduled" so the two sections in Announcement Management never mix."""
    return (
        Announcement.query.filter(
            Announcement.status == Announcement.STATUS_INACTIVE,
            Announcement.published_at.isnot(None),
        )
        .order_by(Announcement.updated_at.desc())
        .all()
    )


def get_active_announcements_for_passengers():
    """What passengers should see: only currently Active announcements.
    Publishes any due Scheduled announcements first, so a passenger never
    misses one just because no admin happened to open the management page."""
    publish_due_announcements()
    return get_published_announcements()