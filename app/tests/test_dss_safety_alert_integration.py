"""Integration tests for the DSS -> Safety Alert announcement flow.

Covers: the "Create Safety Alert" action on the dashboard (only offered for
CAUTION/DELAY and UNSAFE results, and only to Admins), that it merely
prefills the existing Announcement Management "Create Announcement" form
(never auto-publishes), and that once an admin explicitly publishes the
prefilled content it flows through the existing Announcement
service/Activity Log/Operator+Passenger views exactly like any other
announcement.

Run with:
    python -m pytest app/tests/test_dss_safety_alert_integration.py -v
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.activity_log import ActivityLog
from app.models.announcement import Announcement
from app.models.user import User
from app.services import scheduling_service

WINDY_PATCH_TARGET = "app.services.scheduling_service.monitoring_service.get_windy_conditions"


def _windy_reading(wind=10.0, weather="Sunny"):
    now = datetime.now(timezone.utc)
    return {
        "status": "live",
        "reading": SimpleNamespace(
            wind_speed_kmh=wind,
            wind_direction="NE",
            weather_condition=weather,
            temperature_c=28.0,
            recorded_at=now,
            retrieved_at=now,
        ),
        "message": None,
    }


def _make_user(app, admin_id, role, full_name):
    with app.app_context():
        user = User(
            admin_id=admin_id,
            password_hash=generate_password_hash("StartPass123"),
            full_name=full_name,
            role=role,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def _client_as(app, admin_id, role, full_name):
    user_id = _make_user(app, admin_id, role, full_name)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["full_name"] = full_name
        sess["role"] = role
    return client


def _admin(app):
    return _client_as(app, "dss_alert_admin", "Admin", "Test Admin")


def _operator(app):
    return _client_as(app, "dss_alert_operator", "Operator", "Test Operator")


# ---------------------------------------------------------------------------
# Dashboard: when the "Create Safety Alert" action is (and isn't) offered
# ---------------------------------------------------------------------------

def test_dashboard_offers_safety_alert_for_unsafe_result(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with patch(WINDY_PATCH_TARGET, return_value=_windy_reading(weather="Rainy")):
        response = admin_client.get(f"/dashboard?trip_id={trip_id}")

    assert response.status_code == 200
    assert b"UNSAFE" in response.data
    assert b"Create Safety Alert" in response.data


def test_dashboard_offers_safety_alert_for_caution_result(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with patch(WINDY_PATCH_TARGET, return_value=_windy_reading(weather="Cloudy")):
        response = admin_client.get(f"/dashboard?trip_id={trip_id}")

    assert response.status_code == 200
    assert b"CAUTION" in response.data
    assert b"Create Safety Alert" in response.data


def test_dashboard_does_not_offer_safety_alert_for_proceed(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with patch(WINDY_PATCH_TARGET, return_value=_windy_reading(weather="Sunny")):
        response = admin_client.get(f"/dashboard?trip_id={trip_id}")

    assert response.status_code == 200
    assert b"PROCEED" in response.data
    assert b"Create Safety Alert" not in response.data


def test_operator_never_sees_safety_alert_action_even_when_unsafe(app, make_boat, make_trip):
    """Operators can view the dashboard, but the Safety Alert action leads
    into Admin-only Announcement Management, so it must never render for
    them even when the result is UNSAFE."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    operator_client = _operator(app)

    with patch(WINDY_PATCH_TARGET, return_value=_windy_reading(weather="Rainy")):
        response = operator_client.get(f"/dashboard?trip_id={trip_id}")

    assert response.status_code == 200
    assert b"Create Safety Alert" not in response.data


def test_safety_alert_action_does_not_touch_trip_or_manifest(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with patch(WINDY_PATCH_TARGET, return_value=_windy_reading(weather="Rainy")):
        admin_client.get(f"/dashboard?trip_id={trip_id}")

    with app.app_context():
        from app.models.trip import Trip

        trip = db.session.get(Trip, trip_id)
        assert trip.status == "Open"
        assert trip.cancel_reason is None
        assert trip.delay_reason is None


# ---------------------------------------------------------------------------
# The action prefills the existing Create Announcement form; it never
# publishes anything on its own.
# ---------------------------------------------------------------------------

def test_safety_alert_link_prefills_announcement_form_without_publishing(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with app.app_context():
        from app.models.trip import Trip

        trip = db.session.get(Trip, trip_id)
        assessment = scheduling_service.evaluate_conditions(
            _windy_reading(weather="Rainy")["reading"],
            SimpleNamespace(water_level_m=12.1),
        )
        expected_alert = scheduling_service.build_safety_alert(assessment, trip)

    response = admin_client.get(
        "/announcements",
        query_string={
            "prefill_title": expected_alert["title"],
            "prefill_content": expected_alert["content"],
            "prefill_type": expected_alert["type"],
        },
    )

    assert response.status_code == 200
    assert expected_alert["title"].encode() in response.data
    assert b"Unsafe Travel Conditions" in response.data
    assert b'selected' in response.data  # Safety Alert type option pre-selected

    with app.app_context():
        assert Announcement.query.count() == 0  # nothing published/scheduled yet


# ---------------------------------------------------------------------------
# End to end: admin reviews and publishes the generated Safety Alert
# ---------------------------------------------------------------------------

def test_publishing_generated_safety_alert_uses_existing_announcement_flow(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with app.app_context():
        from app.models.trip import Trip

        trip = db.session.get(Trip, trip_id)
        assessment = scheduling_service.evaluate_conditions(
            _windy_reading(weather="Rainy")["reading"],
            SimpleNamespace(water_level_m=12.1),
        )
        generated = scheduling_service.build_safety_alert(assessment, trip)

    response = admin_client.post(
        "/announcements/create",
        data={
            "title": generated["title"],
            "content": generated["content"],
            "type": generated["type"],
            "action": "publish_now",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Announcement published successfully" in response.data

    with app.app_context():
        announcement = Announcement.query.filter_by(title=generated["title"]).first()
        assert announcement is not None
        assert announcement.type == Announcement.TYPE_SAFETY_ALERT
        assert announcement.status == Announcement.STATUS_ACTIVE
        assert announcement.content == generated["content"]

        # Activity Log entry recorded through the existing mechanism.
        logs = ActivityLog.query.filter_by(action="Published Announcement").all()
        assert len(logs) == 1
        assert generated["title"] in logs[0].details

    # Appears in the Operator view.
    operator_client = _operator(app)
    operator_response = operator_client.get("/announcements")
    assert generated["title"].encode() in operator_response.data

    # Appears in the passenger-facing view/API. Note: passenger announcements
    # are actually served by passenger.home() at "/passenger/" (see
    # app/blueprints/passenger/routes.py), which queries Announcement
    # itself and renders passenger/home.html — that's the template that
    # exists and is linked from the app. The announcements blueprint also
    # exposes "/passenger/announcements", but it renders a
    # "passenger/announcements.html" template that isn't present in this
    # codebase, so it isn't used here.
    passenger_response = admin_client.get("/passenger/")
    assert generated["title"].encode() in passenger_response.data

    api_response = admin_client.get("/api/announcements")
    assert api_response.status_code == 200
    assert generated["title"] in api_response.get_json()["data"][0]["title"]


def test_admin_can_edit_generated_alert_before_publishing(app, make_boat, make_trip):
    """The admin must be able to review/edit the generated text before it
    goes live — publishing is only via the normal create route, and any
    text (not just the auto-generated text) is accepted."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin_client = _admin(app)

    with app.app_context():
        from app.models.trip import Trip

        trip = db.session.get(Trip, trip_id)
        assessment = scheduling_service.evaluate_conditions(
            _windy_reading(weather="Rainy")["reading"],
            SimpleNamespace(water_level_m=12.1),
        )
        generated = scheduling_service.build_safety_alert(assessment, trip)

    edited_content = generated["content"] + "\n\nEdited by admin before publishing."

    response = admin_client.post(
        "/announcements/create",
        data={
            "title": generated["title"],
            "content": edited_content,
            "type": generated["type"],
            "action": "publish_now",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        announcement = Announcement.query.filter_by(title=generated["title"]).first()
        assert "Edited by admin before publishing." in announcement.content