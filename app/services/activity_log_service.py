"""Admin-only activity logging.

Only ever pass in human-readable, non-sensitive summaries via `details`.
Never log passwords, API keys, tokens, or other credentials — see the
docstring on ActivityLog itself.
"""
from __future__ import annotations

from app.extensions import db
from app.models.activity_log import ActivityLog

ACTION_ADMIN_LOGIN = "Admin login"
ACTION_ADMIN_LOGOUT = "Admin logout"
ACTION_UPDATED_SAFETY_THRESHOLDS = "Updated Safety Thresholds"
ACTION_UPDATED_REFRESH_INTERVAL = "Updated Refresh Interval"
ACTION_UPDATED_SESSION_SETTINGS = "Updated Session Settings"
ACTION_CHANGED_PASSWORD = "Changed Password"
ACTION_PASSENGER_APPROVED = "Passenger approval"
ACTION_PASSENGER_REJECTED = "Passenger rejection"
ACTION_REPORT_GENERATED = "Report generation"
ACTION_MANIFEST_EDITED = "Edited Passenger Manifest"
ACTION_MANIFEST_DELETED = "Manifest Deleted"
ACTION_MANIFEST_CREATED = "Manifest Created"
ACTION_TRIP_DELAYED = "Trip Delayed"
ACTION_TRIP_CANCELLED = "Trip Cancelled"
ACTION_TIME_SLOT_DELETED = "Time Slot Deleted"
ACTION_TIME_SLOT_CREATED = "Time Slot Created"
ACTION_ADMIN_LOGIN_FAILED = "Failed login attempt"

ACTION_ANNOUNCEMENT_PUBLISHED = "Published Notice"
ACTION_ANNOUNCEMENT_SCHEDULED = "Scheduled Notice"
ACTION_ANNOUNCEMENT_DRAFTED = "Saved Notice Draft"
ACTION_ANNOUNCEMENT_EDITED = "Edited Notice"
ACTION_ANNOUNCEMENT_ACTIVATED = "Published Notice"
ACTION_ANNOUNCEMENT_UNPUBLISHED = "Unpublished Notice"
# Old constant name kept so existing imports keep working; the logged
# wording is now "Unpublished Notice".
ACTION_ANNOUNCEMENT_DEACTIVATED = ACTION_ANNOUNCEMENT_UNPUBLISHED
ACTION_ANNOUNCEMENT_CANCELLED = "Cancelled Scheduled Notice"
ACTION_ANNOUNCEMENT_DELETED = "Deleted Notice"
ACTION_PASSENGER_CANCELLED = "Passenger cancellation"


ACTION_REGISTRATION_SMS_SENT = "Registration SMS Sent"


def _format_threshold_changes(changes: dict) -> str:
    if not changes:
        return "No values were actually changed."
    parts = [f"{key}: {change['from']} -> {change['to']}" for key, change in changes.items()]
    return "; ".join(parts)


def describe_trip(trip) -> str:
    """Readable one-line summary of a trip for Activity Log details.

    Trip.departure_time is stored as naive Philippine wall-clock time, so it
    is formatted as-is (no timezone conversion). Boat and operator are left
    out when the slot has none. Call this BEFORE deleting the trip so the
    log keeps the details after the record is gone.
    """
    if trip is None:
        return ""
    when = trip.departure_time
    parts = [
        f"Scheduled: {when.strftime('%b')} {when.day}, {when.year} "
        f"{when.strftime('%I:%M %p').lstrip('0')}"
    ]
    boat = getattr(trip, "boat", None)
    if boat is not None and boat.name:
        parts.append(f"Boat: {boat.name}")
    if trip.operator_name:
        parts.append(f"Operator: {trip.operator_name}")
    return " | ".join(parts)


def log_action(*, user_id, admin_name: str, action: str, details=None,
               commit: bool = True) -> ActivityLog:
    """Create one Activity Log entry. `details` may be a plain string or a
    dict of before/after values (e.g. from settings_service), which is
    rendered into a readable string before being stored.

    Pass ``commit=False`` to stage the entry in the caller's open
    transaction so the audited change and its log row succeed or fail
    together (used by time slot deletion)."""
    if isinstance(details, dict):
        details_text = _format_threshold_changes(details)
    else:
        details_text = details

    entry = ActivityLog(
        user_id=user_id,
        admin_name=admin_name or "Unknown",
        action=action,
        details=details_text,
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def log_failed_login(*, username, user_id=None,
                     reason: str = "Incorrect username or password.") -> ActivityLog:
    """Record a failed sign-in attempt.

    Only the username/email that was typed is stored, trimmed and length
    capped. The password the person entered must NEVER be passed in or
    logged. Pass ``user_id`` only when the username matched a real account.
    """
    typed = (username or "").strip()[:100] or "(blank)"
    return log_action(
        user_id=user_id,
        admin_name=typed,
        action=ACTION_ADMIN_LOGIN_FAILED,
        details=f"Username entered: {typed} | Reason: {reason}",
    )


def get_recent_logs(limit: int = 50):
    return (
        ActivityLog.query.order_by(
            ActivityLog.created_at.desc(),
            ActivityLog.id.desc(),
        )
        .limit(limit)
        .all()
    )