"""Tests for the Announcement Management module.

Covers: Activity Log integration for admin announcement actions, admin-only
authorization on every management route, and the read-only Operator
Announcements view (including that scheduled/inactive announcements are
never returned to Operators, even by the backend query).

Run with:
    python -m pytest app/tests/test_announcements.py -v
"""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.activity_log import ActivityLog
from app.models.announcement import Announcement
from app.models.user import User
from app.services import announcement_service

# ---------------------------------------------------------------------------
# Local fixtures/helpers (kept in this file, mirroring test_admin_settings.py,
# to avoid changing the shared conftest.py fixtures relied on by other test
# modules).
# ---------------------------------------------------------------------------

def _make_user(app, admin_id, role, password="StartPass123", full_name="Test User"):
    with app.app_context():
        user = User(
            admin_id=admin_id,
            password_hash=generate_password_hash(password),
            full_name=full_name,
            role=role,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def _client_logged_in_as(app, user_id, full_name, role):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["full_name"] = full_name
        sess["role"] = role
    return client


def _admin(app):
    user_id = _make_user(app, "admin_announcements_test", "Admin", full_name="Test Admin")
    return _client_logged_in_as(app, user_id, "Test Admin", "Admin"), user_id


def _operator(app):
    user_id = _make_user(app, "operator_announcements_test", "Operator", full_name="Test Operator")
    return _client_logged_in_as(app, user_id, "Test Operator", "Operator"), user_id


def _make_announcement(app, **kwargs):
    with app.app_context():
        announcement = Announcement(
            title=kwargs.pop("title", "Test Announcement"),
            content=kwargs.pop("content", "Test content"),
            type=kwargs.pop("type", Announcement.TYPE_ANNOUNCEMENT),
            status=kwargs.pop("status", Announcement.STATUS_ACTIVE),
            **kwargs,
        )
        db.session.add(announcement)
        db.session.commit()
        return announcement.id


VALID_PUBLISH_NOW = {
    "title": "Storm Warning",
    "content": "All trips suspended until further notice.",
    "type": Announcement.TYPE_SAFETY_ALERT,
    "action": "publish_now",
}

FUTURE_SCHEDULE = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

VALID_SCHEDULE = {
    "title": "Upcoming Maintenance",
    "content": "Dock will be closed for maintenance.",
    "type": Announcement.TYPE_ANNOUNCEMENT,
    "action": "schedule",
    "scheduled_at": FUTURE_SCHEDULE,
}


# ---------------------------------------------------------------------------
# Admin-only authorization
# ---------------------------------------------------------------------------

def test_unauthenticated_cannot_access_announcements(client):
    response = client.get("/announcements")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_admin_can_access_announcement_management(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/announcements")
    assert response.status_code == 200
    assert b"Create Announcement" in response.data


def test_operator_can_access_readonly_view(app):
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert response.status_code == 200
    assert b"Create Announcement" not in response.data


def test_operator_cannot_create_announcement(app):
    operator_client, _ = _operator(app)
    response = operator_client.post("/announcements/create", data=VALID_PUBLISH_NOW)
    assert response.status_code == 403

    with app.app_context():
        assert Announcement.query.count() == 0


def test_operator_cannot_edit_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, title="Original")

    response = operator_client.post(
        f"/announcements/{announcement_id}/edit",
        data={"title": "Hacked", "content": "x", "type": Announcement.TYPE_ANNOUNCEMENT},
    )
    assert response.status_code == 403

    with app.app_context():
        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.title == "Original"


def test_operator_cannot_delete_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app)

    response = operator_client.post(f"/announcements/{announcement_id}/delete")
    assert response.status_code == 403

    with app.app_context():
        assert db.session.get(Announcement, announcement_id) is not None


def test_operator_cannot_activate_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, status=Announcement.STATUS_INACTIVE)

    response = operator_client.post(f"/announcements/{announcement_id}/activate")
    assert response.status_code == 403

    with app.app_context():
        assert db.session.get(Announcement, announcement_id).status == Announcement.STATUS_INACTIVE


def test_operator_cannot_deactivate_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, status=Announcement.STATUS_ACTIVE)

    response = operator_client.post(f"/announcements/{announcement_id}/deactivate")
    assert response.status_code == 403

    with app.app_context():
        assert db.session.get(Announcement, announcement_id).status == Announcement.STATUS_ACTIVE


def test_unauthenticated_management_routes_redirect_to_login(app, client):
    announcement_id = _make_announcement(app)
    for response in [
        client.post("/announcements/create", data=VALID_PUBLISH_NOW),
        client.post(f"/announcements/{announcement_id}/edit", data={}),
        client.post(f"/announcements/{announcement_id}/delete"),
        client.post(f"/announcements/{announcement_id}/activate"),
        client.post(f"/announcements/{announcement_id}/deactivate"),
    ]:
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Admin actions + Activity Log integration
# ---------------------------------------------------------------------------

def test_publish_now_logs_activity(app):
    admin_client, _ = _admin(app)
    response = admin_client.post("/announcements/create", data=VALID_PUBLISH_NOW, follow_redirects=True)
    assert response.status_code == 200
    assert b"Announcement published successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Published Announcement").all()
        assert len(logs) == 1
        assert logs[0].admin_name == "Test Admin"
        assert "Storm Warning" in logs[0].details

        announcement = Announcement.query.filter_by(title="Storm Warning").first()
        assert announcement.status == Announcement.STATUS_ACTIVE


def test_schedule_logs_activity(app):
    admin_client, _ = _admin(app)
    response = admin_client.post("/announcements/create", data=VALID_SCHEDULE, follow_redirects=True)
    assert response.status_code == 200
    assert b"Announcement scheduled successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Scheduled Announcement").all()
        assert len(logs) == 1
        assert "Upcoming Maintenance" in logs[0].details

        announcement = Announcement.query.filter_by(title="Upcoming Maintenance").first()
        assert announcement.status == Announcement.STATUS_INACTIVE
        assert announcement.published_at is None


def test_edit_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(app, title="Before Edit")

    response = admin_client.post(
        f"/announcements/{announcement_id}/edit",
        data={"title": "After Edit", "content": "Updated content", "type": Announcement.TYPE_ANNOUNCEMENT},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Announcement updated successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Edited Announcement").all()
        assert len(logs) == 1
        assert "After Edit" in logs[0].details

        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.title == "After Edit"


def test_activate_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(app, title="Reactivate Me", status=Announcement.STATUS_INACTIVE)

    response = admin_client.post(f"/announcements/{announcement_id}/activate", follow_redirects=True)
    assert response.status_code == 200
    assert b"Announcement activated successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Activated Announcement").all()
        assert len(logs) == 1
        assert "Reactivate Me" in logs[0].details

        assert db.session.get(Announcement, announcement_id).status == Announcement.STATUS_ACTIVE


def test_deactivate_published_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(
        app, title="Deactivate Me", status=Announcement.STATUS_ACTIVE, published_at=datetime.utcnow()
    )

    response = admin_client.post(f"/announcements/{announcement_id}/deactivate", follow_redirects=True)
    assert response.status_code == 200
    assert b"Announcement deactivated successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Deactivated Announcement").all()
        assert len(logs) == 1
        assert "Deactivate Me" in logs[0].details


def test_cancel_scheduled_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(
        app,
        title="Cancel Me",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() + timedelta(days=1),
        published_at=None,
    )

    response = admin_client.post(f"/announcements/{announcement_id}/deactivate", follow_redirects=True)
    assert response.status_code == 200
    assert b"Scheduled announcement cancelled successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Cancelled Announcement").all()
        assert len(logs) == 1
        assert "Cancel Me" in logs[0].details


def test_delete_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(app, title="Delete Me")

    response = admin_client.post(f"/announcements/{announcement_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert b"Announcement deleted successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Deleted Announcement").all()
        assert len(logs) == 1
        assert "Delete Me" in logs[0].details
        assert db.session.get(Announcement, announcement_id) is None


def test_activity_log_records_correct_admin(app):
    admin_client, admin_id = _admin(app)
    admin_client.post("/announcements/create", data=VALID_PUBLISH_NOW)

    with app.app_context():
        log = ActivityLog.query.filter_by(action="Published Announcement").first()
        assert log.user_id == admin_id
        assert log.admin_name == "Test Admin"


# ---------------------------------------------------------------------------
# Operator read-only view
# ---------------------------------------------------------------------------

def test_operator_view_shows_only_active_announcements(app):
    _make_announcement(app, title="Active One", status=Announcement.STATUS_ACTIVE)
    _make_announcement(
        app,
        title="Scheduled One",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() + timedelta(days=1),
        published_at=None,
    )
    _make_announcement(
        app,
        title="Inactive One",
        status=Announcement.STATUS_INACTIVE,
        published_at=datetime.utcnow(),
    )

    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert response.status_code == 200
    assert b"Active One" in response.data
    assert b"Scheduled One" not in response.data
    assert b"Inactive One" not in response.data


def test_operator_view_enforces_active_only_in_backend_query(app):
    """Even if a scheduled/inactive announcement exists, the backend query
    used for the Operator view must never fetch it — not just hide it in
    the template."""
    _make_announcement(
        app,
        title="Hidden Scheduled",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() + timedelta(days=1),
        published_at=None,
    )

    with app.app_context():
        published = announcement_service.get_published_announcements()
        assert published == []


def test_operator_view_has_no_management_controls(app):
    _make_announcement(app, title="Active One", status=Announcement.STATUS_ACTIVE)
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")

    for control in [b"Edit", b"Deactivate", b"Delete", b"Activate", b"Schedule", b"Publish Now"]:
        assert control not in response.data


def test_operator_view_indicates_readonly(app):
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert b"Read-only" in response.data


def test_operator_view_shows_newly_activated_announcement(app):
    """After an admin publishes an announcement, it should show up on the
    Operator view (same underlying data, no separate model/service)."""
    admin_client, _ = _admin(app)
    admin_client.post("/announcements/create", data=VALID_PUBLISH_NOW)

    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert b"Storm Warning" in response.data


# ---------------------------------------------------------------------------
# Automatic scheduled publishing (no admin refresh required)
# ---------------------------------------------------------------------------

def test_scheduled_announcement_auto_publishes_when_due(app):
    announcement_id = _make_announcement(
        app,
        title="Due Announcement",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() - timedelta(minutes=1),  # already due
        published_at=None,
    )

    # Simply loading the Operator view (no admin page refresh, no manual
    # publish action) should be enough to publish it.
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert response.status_code == 200
    assert b"Due Announcement" in response.data

    with app.app_context():
        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.status == Announcement.STATUS_ACTIVE
        assert announcement.published_at is not None