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

ACTION_ANNOUNCEMENT_PUBLISHED = "Published Announcement"
ACTION_ANNOUNCEMENT_SCHEDULED = "Scheduled Announcement"
ACTION_ANNOUNCEMENT_EDITED = "Edited Announcement"
ACTION_ANNOUNCEMENT_ACTIVATED = "Activated Announcement"
ACTION_ANNOUNCEMENT_DEACTIVATED = "Deactivated Announcement"
ACTION_ANNOUNCEMENT_CANCELLED = "Cancelled Announcement"
ACTION_ANNOUNCEMENT_DELETED = "Deleted Announcement"


def _format_threshold_changes(changes: dict) -> str:
    if not changes:
        return "No values were actually changed."
    parts = [f"{key}: {change['from']} -> {change['to']}" for key, change in changes.items()]
    return "; ".join(parts)


def log_action(*, user_id, admin_name: str, action: str, details=None) -> ActivityLog:
    """Create one Activity Log entry. `details` may be a plain string or a
    dict of before/after values (e.g. from settings_service), which is
    rendered into a readable string before being stored."""
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
    db.session.commit()
    return entry


def get_recent_logs(limit: int = 50):
    return (
        ActivityLog.query.order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )