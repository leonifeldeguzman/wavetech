"""Admin-only activity logging.

Only ever pass in human-readable, non-sensitive summaries via `details`.
Never log passwords, API keys, tokens, or other credentials — see the
docstring on ActivityLog itself.
"""
from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import func

from app.extensions import db
from app.models.activity_log import ActivityLog
from app.utils.timezone import utc_now_naive

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
ACTION_TRIP_DEPARTED = "Trip Departed"
ACTION_TIME_SLOT_DELETED = "Time Slot Deleted"
ACTION_TIME_SLOT_CREATED = "Time Slot Created"
ACTION_ADMIN_LOGIN_FAILED = "Failed login attempt"
ACTION_ADMIN_LOGIN_LOCKED = "Login locked out"

# Login lockout: after LOGIN_MAX_FAILED_ATTEMPTS failed attempts for the same
# Admin ID within LOGIN_FAILURE_WINDOW_MINUTES, that Admin ID cannot sign in
# for LOGIN_LOCKOUT_MINUTES (even with the correct password).
LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_FAILURE_WINDOW_MINUTES = 15
LOGIN_LOCKOUT_MINUTES = 15

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


# ---------------------------------------------------------------------------
# Login lockout (state is kept in the activity log itself, so no new table)
# ---------------------------------------------------------------------------

def _typed_id(username) -> str:
    return (username or "").strip()[:100]


def get_login_lockout_minutes(username) -> int:
    """Minutes left on an active lockout for this Admin ID, or 0 if none."""
    typed = _typed_id(username)
    if not typed:
        return 0

    now = utc_now_naive()
    latest = (
        ActivityLog.query.filter(
            ActivityLog.action == ACTION_ADMIN_LOGIN_LOCKED,
            func.lower(ActivityLog.admin_name) == typed.lower(),
            ActivityLog.created_at >= now - timedelta(minutes=LOGIN_LOCKOUT_MINUTES),
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .first()
    )
    if latest is None or latest.created_at is None:
        return 0

    unlock_at = latest.created_at + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    seconds_left = (unlock_at - now).total_seconds()
    return max(0, math.ceil(seconds_left / 60))


def count_recent_failed_logins(username, user_id=None) -> int:
    """Failed attempts for this Admin ID that still count toward a lockout.

    Counting starts at the latest of: the start of the time window, the last
    lockout for this Admin ID, and the account's last successful login (so a
    successful sign-in resets the counter).
    """
    typed = _typed_id(username)
    if not typed:
        return 0

    since = utc_now_naive() - timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES)

    last_lock = (
        db.session.query(func.max(ActivityLog.created_at))
        .filter(
            ActivityLog.action == ACTION_ADMIN_LOGIN_LOCKED,
            func.lower(ActivityLog.admin_name) == typed.lower(),
        )
        .scalar()
    )
    if last_lock and last_lock > since:
        since = last_lock

    if user_id is not None:
        last_ok = (
            db.session.query(func.max(ActivityLog.created_at))
            .filter(
                ActivityLog.user_id == user_id,
                ActivityLog.action == ACTION_ADMIN_LOGIN,
            )
            .scalar()
        )
        if last_ok and last_ok > since:
            since = last_ok

    return (
        ActivityLog.query.filter(
            ActivityLog.action == ACTION_ADMIN_LOGIN_FAILED,
            func.lower(ActivityLog.admin_name) == typed.lower(),
            ActivityLog.created_at > since,
        ).count()
    )


def lock_login_if_needed(*, username, user_id=None) -> int:
    """Call right after a failed attempt is logged. Starts a lockout when the
    limit is reached and returns the lockout length in minutes (0 if the
    Admin ID is not locked)."""
    if count_recent_failed_logins(username, user_id) < LOGIN_MAX_FAILED_ATTEMPTS:
        return 0

    typed = _typed_id(username) or "(blank)"
    log_action(
        user_id=user_id,
        admin_name=typed,
        action=ACTION_ADMIN_LOGIN_LOCKED,
        details=(
            f"Admin ID: {typed} | Locked for {LOGIN_LOCKOUT_MINUTES} minutes "
            f"after {LOGIN_MAX_FAILED_ATTEMPTS} failed attempts."
        ),
    )
    return LOGIN_LOCKOUT_MINUTES


def get_recent_logs(limit: int = 50):
    return (
        ActivityLog.query.order_by(
            ActivityLog.created_at.desc(),
            ActivityLog.id.desc(),
        )
        .limit(limit)
        .all()
    )