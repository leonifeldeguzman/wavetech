"""Tests for the Notice (formerly Announcement) Management module.

Covers: Activity Log integration for announcement actions, that Admin and
Operator accounts share identical Announcement Management access (every
management route only requires an authenticated session), and the
background scheduler that automatically publishes Scheduled announcements
without any admin/operator page load or refresh.

Run with:
    python -m pytest app/tests/test_announcements.py -v
"""
import threading
import time
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
# Shared Admin/Operator authorization
# ---------------------------------------------------------------------------

def test_unauthenticated_cannot_access_announcements(client):
    response = client.get("/announcements")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_admin_can_access_announcement_management(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/announcements")
    assert response.status_code == 200
    assert b"Create Notice" in response.data


def test_operator_can_access_announcement_management(app):
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")
    assert response.status_code == 200
    assert b"Create Notice" in response.data


def test_operator_can_create_announcement(app):
    operator_client, _ = _operator(app)
    response = operator_client.post(
        "/announcements/create", data=VALID_PUBLISH_NOW, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"Notice published successfully" in response.data

    with app.app_context():
        assert Announcement.query.count() == 1


def test_operator_can_edit_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, title="Original")

    response = operator_client.post(
        f"/announcements/{announcement_id}/edit",
        data={"title": "Updated", "content": "x", "type": Announcement.TYPE_ANNOUNCEMENT},
    )
    assert response.status_code == 302

    with app.app_context():
        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.title == "Updated"


def test_operator_can_delete_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app)

    response = operator_client.post(f"/announcements/{announcement_id}/delete")
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(Announcement, announcement_id) is None


def test_operator_can_activate_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, status=Announcement.STATUS_INACTIVE)

    response = operator_client.post(f"/announcements/{announcement_id}/activate")
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(Announcement, announcement_id).status == Announcement.STATUS_ACTIVE


def test_operator_can_deactivate_announcement(app):
    operator_client, _ = _operator(app)
    announcement_id = _make_announcement(app, status=Announcement.STATUS_ACTIVE)

    response = operator_client.post(f"/announcements/{announcement_id}/deactivate")
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(Announcement, announcement_id).status == Announcement.STATUS_INACTIVE


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
    assert b"Notice published successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Published Notice").all()
        assert len(logs) == 1
        assert logs[0].admin_name == "Test Admin"
        assert "Storm Warning" in logs[0].details

        announcement = Announcement.query.filter_by(title="Storm Warning").first()
        assert announcement.status == Announcement.STATUS_ACTIVE


def test_schedule_logs_activity(app):
    admin_client, _ = _admin(app)
    response = admin_client.post("/announcements/create", data=VALID_SCHEDULE, follow_redirects=True)
    assert response.status_code == 200
    assert b"Notice scheduled successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Scheduled Notice").all()
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
    assert b"Notice updated successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Edited Notice").all()
        assert len(logs) == 1
        assert "After Edit" in logs[0].details

        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.title == "After Edit"


def test_activate_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(app, title="Reactivate Me", status=Announcement.STATUS_INACTIVE)

    response = admin_client.post(f"/announcements/{announcement_id}/activate", follow_redirects=True)
    assert response.status_code == 200
    assert b"Notice published successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Published Notice").all()
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
    assert b"Notice unpublished successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Unpublished Notice").all()
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
    assert b"Scheduled notice cancelled" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Cancelled Scheduled Notice").all()
        assert len(logs) == 1
        assert "Cancel Me" in logs[0].details


def test_delete_logs_activity(app):
    admin_client, _ = _admin(app)
    announcement_id = _make_announcement(app, title="Delete Me")

    response = admin_client.post(f"/announcements/{announcement_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert b"Notice deleted successfully" in response.data

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Deleted Notice").all()
        assert len(logs) == 1
        assert "Delete Me" in logs[0].details
        assert db.session.get(Announcement, announcement_id) is None


def test_activity_log_records_correct_admin(app):
    admin_client, admin_id = _admin(app)
    admin_client.post("/announcements/create", data=VALID_PUBLISH_NOW)

    with app.app_context():
        log = ActivityLog.query.filter_by(action="Published Notice").first()
        assert log.user_id == admin_id
        assert log.admin_name == "Test Admin"


# ---------------------------------------------------------------------------
# Operator view parity with Admin
# ---------------------------------------------------------------------------

def test_operator_view_shows_scheduled_and_inactive_announcements(app):
    """Operators see the same full Announcement Management view as Admin,
    including Scheduled and Inactive announcements (not just Active)."""
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
    assert b"Scheduled One" in response.data
    assert b"Inactive One" in response.data


def test_get_published_announcements_returns_active_only(app):
    """The Published Announcements section (shown to both roles) is backed
    by a query that only ever returns Active announcements."""
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


def test_operator_view_has_management_controls(app):
    _make_announcement(app, title="Active One", status=Announcement.STATUS_ACTIVE)
    operator_client, _ = _operator(app)
    response = operator_client.get("/announcements")

    assert b"Create Notice" in response.data


def test_operator_view_shows_newly_activated_announcement(app):
    """After publishing an announcement, it should show up on the Operator
    view (same underlying data, no separate model/service)."""
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


# ---------------------------------------------------------------------------
# Background scheduler — publishes without any admin/operator page load
# ---------------------------------------------------------------------------

def test_scheduler_is_not_started_automatically_during_tests(app):
    """create_app() must not start a real background thread while
    TESTING=True — tests control publishing explicitly (via routes or by
    calling start_background_scheduler themselves)."""
    scheduler_threads = [
        t for t in threading.enumerate() if t.name == "announcement-scheduler"
    ]
    assert scheduler_threads == []


def test_background_scheduler_publishes_without_any_page_load(app):
    """The scheduler thread itself — not any route's fallback call — must
    flip a due Scheduled announcement to Active and stamp published_at."""
    announcement_id = _make_announcement(
        app,
        title="Background Publish Me",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() + timedelta(seconds=1),
        published_at=None,
    )

    try:
        started = announcement_service.start_background_scheduler(
            app, interval_seconds=1
        )
        assert started is True

        # Give the thread a few ticks to notice the announcement is due
        # and publish it — no HTTP request made anywhere in this test.
        # Each poll uses a fresh app context (and therefore a fresh
        # session) so it always sees the latest committed state.
        status = None
        published_at = None
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.5)
            with app.app_context():
                announcement = db.session.get(Announcement, announcement_id)
                status = announcement.status
                published_at = announcement.published_at
            if status == Announcement.STATUS_ACTIVE:
                break

        assert status == Announcement.STATUS_ACTIVE
        assert published_at is not None
    finally:
        announcement_service.stop_background_scheduler()


def test_starting_scheduler_twice_does_not_create_a_second_instance(app):
    try:
        first_started = announcement_service.start_background_scheduler(
            app, interval_seconds=30
        )
        second_started = announcement_service.start_background_scheduler(
            app, interval_seconds=30
        )
        assert first_started is True
        assert second_started is False

        scheduler_threads = [
            t for t in threading.enumerate() if t.name == "announcement-scheduler"
        ]
        assert len(scheduler_threads) == 1
    finally:
        announcement_service.stop_background_scheduler()


def test_stop_scheduler_actually_stops_the_thread(app):
    announcement_service.start_background_scheduler(app, interval_seconds=30)
    announcement_service.stop_background_scheduler()

    scheduler_threads = [
        t for t in threading.enumerate() if t.name == "announcement-scheduler"
    ]
    assert scheduler_threads == []


def test_background_scheduler_keeps_philippine_time_display_conversion(app):
    """The background scheduler reuses publish_due_announcements(), which
    only ever writes naive UTC to published_at — the ph_time template
    filter is what converts it for display, unchanged by this feature."""
    announcement_id = _make_announcement(
        app,
        title="PHT Display Check",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() - timedelta(minutes=1),
        published_at=None,
    )

    with app.app_context():
        announcement_service.publish_due_announcements()
        announcement = db.session.get(Announcement, announcement_id)
        assert announcement.published_at.tzinfo is None  # stored naive UTC

    admin_client, _ = _admin(app)
    response = admin_client.get("/announcements")
    assert response.status_code == 200
    assert b"PHT" in response.data

# ===========================================================================
# QA fixes: Notice types, Unpublish/Expire lifecycle, expiry enforcement
# ===========================================================================

def _utc_to_ph_input(dt_utc):
    """Format a naive-UTC datetime the way the admin form submits it
    (Philippine Time, datetime-local format)."""
    from app.utils.timezone import to_ph_time
    return to_ph_time(dt_utc).strftime("%Y-%m-%dT%H:%M")


def _future_ph(**delta):
    return _utc_to_ph_input(datetime.utcnow() + timedelta(**delta))


def _past_ph(**delta):
    return _utc_to_ph_input(datetime.utcnow() - timedelta(**delta))


def _post_create(client, **overrides):
    data = dict(VALID_PUBLISH_NOW)
    data.update(overrides)
    return client.post("/announcements/create", data=data, follow_redirects=True)


# ---- Notice types ---------------------------------------------------------

def test_all_four_notice_types_can_be_created(app):
    admin_client, _ = _admin(app)
    for notice_type in ("Safety Alert", "Delay", "Cancellation", "General Notice"):
        response = _post_create(admin_client, title=f"T {notice_type}", type=notice_type)
        assert b"Notice published successfully" in response.data

    with app.app_context():
        assert {a.type for a in Announcement.query.all()} == {
            "Safety Alert", "Delay", "Cancellation", "General Notice"
        }


def test_invalid_notice_type_is_rejected(app):
    admin_client, _ = _admin(app)
    _post_create(admin_client, type="Bogus")
    with app.app_context():
        assert Announcement.query.count() == 0


def test_legacy_announcement_type_is_normalized_to_general_notice(app):
    admin_client, _ = _admin(app)
    _post_create(admin_client, type="Announcement")
    with app.app_context():
        assert Announcement.query.one().type == "General Notice"


def test_type_slugs_drive_color_coding(app):
    with app.app_context():
        slugs = {
            t: Announcement(title="x", content="y", type=t).type_slug
            for t in Announcement.ALL_TYPES
        }
    assert slugs == {
        "Safety Alert": "safety",
        "Delay": "delay",
        "Cancellation": "cancellation",
        "General Notice": "general",
    }


# ---- Terminology ----------------------------------------------------------

def test_admin_page_uses_notice_terminology_not_deactivate(app):
    _make_announcement(app, title="Live", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow())
    admin_client, _ = _admin(app)
    html = admin_client.get("/announcements").data.decode()

    assert "NOTICE MANAGEMENT" in html
    assert "Create Notice" in html
    assert "Unpublish" in html
    assert "Deactivate" not in html
    assert "Announcement" not in html


def test_passenger_pages_use_notices_terminology(app, client):
    _make_announcement(app, title="Live", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow())
    html = client.get("/passenger/announcements").data.decode()
    assert "All Notices" in html
    assert "Announcement" not in html


# ---- Expiry validation ----------------------------------------------------

def test_publish_now_with_past_expiry_is_rejected(app):
    admin_client, _ = _admin(app)
    response = _post_create(admin_client, expires_at=_past_ph(hours=1))
    assert b"Expiry date/time must be in the future" in response.data
    with app.app_context():
        assert Announcement.query.count() == 0


def test_expiry_is_optional(app):
    admin_client, _ = _admin(app)
    _post_create(admin_client)
    with app.app_context():
        assert Announcement.query.one().expires_at is None


def test_expiry_is_stored_as_utc_from_philippine_time_input(app):
    admin_client, _ = _admin(app)
    _post_create(admin_client, expires_at=_future_ph(days=1))
    with app.app_context():
        stored = Announcement.query.one().expires_at
        assert timedelta(hours=23) < stored - datetime.utcnow() < timedelta(hours=25)


def test_scheduled_expiry_must_be_after_publish_time(app):
    admin_client, _ = _admin(app)
    response = admin_client.post(
        "/announcements/create",
        data={
            **VALID_SCHEDULE,
            "scheduled_at": _future_ph(days=2),
            "expires_at": _future_ph(days=1),
        },
        follow_redirects=True,
    )
    assert b"must be after the scheduled publish time" in response.data
    with app.app_context():
        assert Announcement.query.count() == 0


def test_invalid_expiry_format_is_rejected(app):
    admin_client, _ = _admin(app)
    response = _post_create(admin_client, expires_at="not-a-date")
    assert b"Expiry date/time is invalid" in response.data


def test_edit_cannot_set_expiry_in_the_past(app):
    admin_client, _ = _admin(app)
    nid = _make_announcement(app, status=Announcement.STATUS_ACTIVE,
                             published_at=datetime.utcnow())
    response = admin_client.post(
        f"/announcements/{nid}/edit",
        data={"title": "t", "content": "c", "type": "Delay",
              "expires_at": _past_ph(hours=1)},
        follow_redirects=True,
    )
    assert b"must be in the future" in response.data
    with app.app_context():
        assert db.session.get(Announcement, nid).expires_at is None


def test_edit_can_clear_expiry(app):
    admin_client, _ = _admin(app)
    nid = _make_announcement(app, status=Announcement.STATUS_ACTIVE,
                             published_at=datetime.utcnow(),
                             expires_at=datetime.utcnow() + timedelta(days=1))
    admin_client.post(
        f"/announcements/{nid}/edit",
        data={"title": "t", "content": "c", "type": "Delay", "expires_at": ""},
    )
    with app.app_context():
        assert db.session.get(Announcement, nid).expires_at is None


def test_edit_without_expiry_field_leaves_expiry_untouched(app):
    admin_client, _ = _admin(app)
    expiry = datetime.utcnow() + timedelta(days=1)
    nid = _make_announcement(app, status=Announcement.STATUS_ACTIVE,
                             published_at=datetime.utcnow(), expires_at=expiry)
    admin_client.post(
        f"/announcements/{nid}/edit",
        data={"title": "t", "content": "c", "type": "Delay"},
    )
    with app.app_context():
        assert db.session.get(Announcement, nid).expires_at == expiry


def test_expired_notice_text_can_still_be_edited_with_unchanged_expiry(app):
    admin_client, _ = _admin(app)
    expiry = datetime.utcnow() - timedelta(hours=2)
    nid = _make_announcement(app, status=Announcement.STATUS_ACTIVE,
                             published_at=datetime.utcnow() - timedelta(days=1),
                             expires_at=expiry)
    admin_client.post(
        f"/announcements/{nid}/edit",
        data={"title": "Fixed typo", "content": "c", "type": "Delay",
              "expires_at": _utc_to_ph_input(expiry)},
    )
    with app.app_context():
        notice = db.session.get(Announcement, nid)
        assert notice.title == "Fixed typo"
        assert notice.display_status == "Expired"


# ---- Expiry enforcement (backend) -----------------------------------------

def test_expired_notice_is_not_returned_to_passengers(app):
    _make_announcement(app, title="Still Live", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow(),
                       expires_at=datetime.utcnow() + timedelta(hours=1))
    _make_announcement(app, title="No Expiry", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow())
    _make_announcement(app, title="Gone Stale", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow() - timedelta(days=1),
                       expires_at=datetime.utcnow() - timedelta(minutes=1))

    with app.app_context():
        titles = {a.title for a in announcement_service.get_published_announcements()}
        assert titles == {"Still Live", "No Expiry"}
        titles = {
            a.title
            for a in announcement_service.get_active_announcements_for_passengers()
        }
        assert titles == {"Still Live", "No Expiry"}


def test_expired_notice_hidden_on_every_passenger_surface(app, client):
    _make_announcement(app, title="Gone Stale", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow() - timedelta(days=1),
                       expires_at=datetime.utcnow() - timedelta(minutes=1))
    _make_announcement(app, title="Visible One", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow())

    for url in ("/passenger/announcements", "/api/announcements"):
        body = client.get(url).data
        assert b"Visible One" in body, url
        assert b"Gone Stale" not in body, url


def test_expired_notice_is_kept_and_listed_in_admin_history(app):
    nid = _make_announcement(
        app, title="Gone Stale", status=Announcement.STATUS_ACTIVE,
        published_at=datetime.utcnow() - timedelta(days=1),
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    admin_client, _ = _admin(app)
    html = admin_client.get("/announcements").data.decode()

    assert "Gone Stale" in html
    assert "<h2>Expired</h2>" in html
    with app.app_context():
        notice = db.session.get(Announcement, nid)
        assert notice is not None  # never deleted
        assert notice.display_status == "Expired"
        assert announcement_service.get_expired_announcements() == [notice]


def test_notice_expires_without_any_admin_action_or_scheduler(app):
    """Expiry is time-based at query time: no job has to run."""
    nid = _make_announcement(
        app, title="Short Lived", status=Announcement.STATUS_ACTIVE,
        published_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(seconds=1),
    )
    with app.app_context():
        assert len(announcement_service.get_published_announcements()) == 1

    time.sleep(1.2)

    with app.app_context():
        assert announcement_service.get_published_announcements() == []
        assert db.session.get(Announcement, nid).display_status == "Expired"


# ---- Publishing lifecycle -------------------------------------------------

def test_draft_is_hidden_from_passengers_and_editable(app, client):
    admin_client, _ = _admin(app)
    response = _post_create(admin_client, action="save_draft", title="My Draft")
    assert b"saved as a draft" in response.data

    with app.app_context():
        notice = Announcement.query.one()
        assert notice.display_status == "Draft"
        nid = notice.id

    assert b"My Draft" not in client.get("/passenger/announcements").data

    admin_client.post(
        f"/announcements/{nid}/edit",
        data={"title": "Edited Draft", "content": "c", "type": "Delay"},
    )
    with app.app_context():
        assert db.session.get(Announcement, nid).title == "Edited Draft"


def test_publish_unpublish_roundtrip_controls_passenger_visibility(app, client):
    admin_client, _ = _admin(app)
    _post_create(admin_client, title="Cycle Me", action="save_draft")
    with app.app_context():
        nid = Announcement.query.one().id

    admin_client.post(f"/announcements/{nid}/activate")
    assert b"Cycle Me" in client.get("/passenger/announcements").data

    admin_client.post(f"/announcements/{nid}/deactivate")
    assert b"Cycle Me" not in client.get("/passenger/announcements").data
    with app.app_context():
        assert db.session.get(Announcement, nid).display_status == "Unpublished"

    admin_client.post(f"/announcements/{nid}/activate")
    assert b"Cycle Me" in client.get("/passenger/announcements").data


def test_cannot_publish_a_notice_whose_expiry_has_passed(app, client):
    admin_client, _ = _admin(app)
    nid = _make_announcement(
        app, title="Too Late", status=Announcement.STATUS_INACTIVE,
        published_at=datetime.utcnow() - timedelta(days=1),
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    response = admin_client.post(f"/announcements/{nid}/activate", follow_redirects=True)
    assert b"expiry date/time has already passed" in response.data
    assert b"Too Late" not in client.get("/passenger/announcements").data
    with app.app_context():
        assert db.session.get(Announcement, nid).status == Announcement.STATUS_INACTIVE


def test_cancelling_a_schedule_returns_it_to_draft_and_it_never_auto_publishes(app):
    admin_client, _ = _admin(app)
    nid = _make_announcement(
        app, title="Cancel Me",
        status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() - timedelta(minutes=5),
        published_at=None,
    )
    admin_client.post(f"/announcements/{nid}/deactivate")

    with app.app_context():
        assert announcement_service.publish_due_announcements() == 0
        notice = db.session.get(Announcement, nid)
        assert notice.display_status == "Draft"
        assert notice.status == Announcement.STATUS_INACTIVE


def test_auto_published_scheduled_notice_with_passed_expiry_is_not_texted(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        announcement_service, "dispatch_announcement_sms", lambda i: sent.append(i)
    )
    nid = _make_announcement(
        app, title="Missed Window", status=Announcement.STATUS_INACTIVE,
        scheduled_at=datetime.utcnow() - timedelta(hours=2),
        expires_at=datetime.utcnow() - timedelta(hours=1),
        published_at=None,
    )
    with app.app_context():
        announcement_service.publish_due_announcements()
        assert db.session.get(Announcement, nid).display_status == "Expired"
    assert sent == []


def test_display_status_values(app):
    now = datetime.utcnow()
    with app.app_context():
        mk = lambda **k: Announcement(title="x", content="y", **k)
        assert mk(status="Inactive").display_status == "Draft"
        assert mk(status="Inactive", scheduled_at=now).display_status == "Scheduled"
        assert mk(status="Active", published_at=now).display_status == "Published"
        assert mk(status="Inactive", published_at=now).display_status == "Unpublished"
        assert mk(status="Active", published_at=now,
                  expires_at=now - timedelta(seconds=1)).display_status == "Expired"


# ---- Passenger rendering --------------------------------------------------

def test_passenger_page_shows_type_badge_and_color_class_for_each_type(app, client):
    for t in Announcement.ALL_TYPES:
        _make_announcement(app, title=f"Title {t}", type=t,
                           status=Announcement.STATUS_ACTIVE,
                           published_at=datetime.utcnow())
    html = client.get("/passenger/announcements").data.decode()
    for t, slug in (("Safety Alert", "safety"), ("Delay", "delay"),
                    ("Cancellation", "cancellation"), ("General Notice", "general")):
        assert f"home-announcement-type {slug}" in html
        assert t in html


def test_passenger_home_hides_expired_and_unpublished(app, client):
    _make_announcement(app, title="Gone Stale", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow() - timedelta(days=1),
                       expires_at=datetime.utcnow() - timedelta(minutes=1))
    _make_announcement(app, title="Pulled Back", status=Announcement.STATUS_INACTIVE,
                       published_at=datetime.utcnow())
    _make_announcement(app, title="Shown Notice", status=Announcement.STATUS_ACTIVE,
                       published_at=datetime.utcnow())
    response = client.get("/passenger/")
    assert response.status_code == 200
    body = response.data
    assert b"Shown Notice" in body
    assert b"Gone Stale" not in body
    assert b"Pulled Back" not in body
