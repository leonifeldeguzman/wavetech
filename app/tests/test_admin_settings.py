"""Tests for the Admin Settings module.

Covers: Account (profile/change-password/logout), Safety Thresholds,
Refresh Interval, and Security & Activity (session settings + activity
log), including access control between Admin and Operator roles.

Run with:
    python -m pytest app/tests/test_admin_settings.py -v
"""
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models.activity_log import ActivityLog
from app.models.pending_registration import PendingRegistration
from app.models.user import User
from app.services import scheduling_service, settings_service


# ---------------------------------------------------------------------------
# Local fixtures/helpers (kept in this file to avoid changing the shared
# conftest.py fixtures relied on by other test modules).
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
    user_id = _make_user(app, "admin_settings_test", "Admin", full_name="Test Admin")
    return _client_logged_in_as(app, user_id, "Test Admin", "Admin"), user_id


def _operator(app):
    user_id = _make_user(app, "operator_settings_test", "Operator", full_name="Test Operator")
    return _client_logged_in_as(app, user_id, "Test Operator", "Operator"), user_id


VALID_THRESHOLDS = {
    "wind_safe_max_kmh": "20",
    "wind_caution_max_kmh": "30",
    "water_safe_min_m": "10.50",
    "water_safe_max_m": "12.50",
    "water_caution_min_m": "10.00",
    "water_caution_max_m": "13.00",
    "weather_safe": "Sunny",
    "weather_caution": "Cloudy",
    "weather_unsafe": "Rainy",
}


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def test_unauthenticated_cannot_access_settings(client):
    response = client.get("/settings")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_profile_access_shows_account_info(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert b"admin_settings_test" in response.data
    assert b"Test Admin" in response.data


def test_valid_password_change(app):
    admin_client, user_id = _admin(app)
    response = admin_client.post(
        "/settings/change-password",
        data={
            "current_password": "StartPass123",
            "new_password": "BrandNewPass456",
            "confirm_password": "BrandNewPass456",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Password updated successfully" in response.data

    with app.app_context():
        user = db.session.get(User, user_id)
        from app.utils.security import verify_password
        assert verify_password("BrandNewPass456", user.password_hash)
        assert not verify_password("StartPass123", user.password_hash)


def test_password_change_incorrect_current_password(app):
    admin_client, user_id = _admin(app)
    response = admin_client.post(
        "/settings/change-password",
        data={
            "current_password": "WrongPassword",
            "new_password": "BrandNewPass456",
            "confirm_password": "BrandNewPass456",
        },
        follow_redirects=True,
    )
    assert b"Current password is incorrect" in response.data

    with app.app_context():
        user = db.session.get(User, user_id)
        from app.utils.security import verify_password
        assert verify_password("StartPass123", user.password_hash)


def test_password_change_confirmation_mismatch(app):
    admin_client, user_id = _admin(app)
    response = admin_client.post(
        "/settings/change-password",
        data={
            "current_password": "StartPass123",
            "new_password": "BrandNewPass456",
            "confirm_password": "SomethingElse789",
        },
        follow_redirects=True,
    )
    assert b"do not match" in response.data

    with app.app_context():
        user = db.session.get(User, user_id)
        from app.utils.security import verify_password
        assert verify_password("StartPass123", user.password_hash)


def test_logout_clears_session_and_uses_existing_route(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

    # Session is gone: protected pages now bounce to login.
    response = admin_client.get("/settings")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_logs_activity(app):
    with app.app_context():
        user = User(
            admin_id="login_log_test",
            password_hash=generate_password_hash("StartPass123"),
            full_name="Login Logger",
            role="Admin",
        )
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"admin_id": "login_log_test", "password": "StartPass123"})

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Admin login").all()
        assert len(logs) == 1
        assert logs[0].admin_name == "Login Logger"


# ---------------------------------------------------------------------------
# Safety Thresholds
# ---------------------------------------------------------------------------

def test_admin_can_access_thresholds_form(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/settings")
    assert b'name="wind_safe_max_kmh"' in response.data


def test_operator_cannot_view_thresholds_form(app):
    operator_client, _ = _operator(app)
    response = operator_client.get("/settings")
    assert response.status_code == 200
    assert b'name="wind_safe_max_kmh"' not in response.data
    assert b"Only Admin accounts can view and modify Safety Thresholds" in response.data


def test_admin_can_update_thresholds_and_they_persist(app):
    admin_client, _ = _admin(app)
    response = admin_client.post(
        "/settings/safety-thresholds",
        data={**VALID_THRESHOLDS, "wind_safe_max_kmh": "18"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Safety thresholds updated successfully" in response.data

    with app.app_context():
        thresholds = settings_service.get_safety_thresholds()
        assert thresholds["wind_safe_max_kmh"] == 18.0


def test_scheduling_decision_support_uses_updated_thresholds(app):
    admin_client, _ = _admin(app)
    # Lower the wind safe max from the default (20) to 5, so a wind speed
    # of 10 km/h — previously "Safe" — should now read as "Caution".
    admin_client.post(
        "/settings/safety-thresholds",
        data={**VALID_THRESHOLDS, "wind_safe_max_kmh": "5", "wind_caution_max_kmh": "15"},
    )

    with app.app_context():
        from types import SimpleNamespace
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        windy_reading = SimpleNamespace(
            wind_speed_kmh=10.0,
            wind_direction="NE",
            weather_condition="Sunny",
            temperature_c=28.0,
            retrieved_at=now,
        )
        llda_reading = SimpleNamespace(water_level_m=12.1, recorded_at=now)

        result = scheduling_service.evaluate_conditions(windy_reading, llda_reading)
        assert result.wind_speed.status == scheduling_service.STATUS_CAUTION
        assert result.recommendation == scheduling_service.RECOMMENDATION_CAUTION


def test_invalid_threshold_values_rejected(app):
    admin_client, _ = _admin(app)

    with app.app_context():
        before = settings_service.get_safety_thresholds()

    cases = [
        {**VALID_THRESHOLDS, "wind_safe_max_kmh": "-5"},  # negative wind speed
        {**VALID_THRESHOLDS, "wind_caution_max_kmh": "10"},  # caution <= safe
        {**VALID_THRESHOLDS, "water_safe_min_m": ""},  # empty required value
        {**VALID_THRESHOLDS, "water_caution_min_m": "11.00"},  # doesn't surround safe range
        {**VALID_THRESHOLDS, "weather_safe": "Sunny", "weather_caution": "Sunny, Cloudy"},  # overlap
        {**VALID_THRESHOLDS, "weather_unsafe": ""},  # invalid weather configuration
    ]

    for payload in cases:
        response = admin_client.post(
            "/settings/safety-thresholds", data=payload, follow_redirects=True
        )
        assert response.status_code == 200

    with app.app_context():
        after = settings_service.get_safety_thresholds()
        assert before == after


def test_unauthorized_cannot_modify_thresholds(app, client):
    response = client.post("/settings/safety-thresholds", data=VALID_THRESHOLDS)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_operator_cannot_modify_thresholds(app):
    operator_client, _ = _operator(app)

    with app.app_context():
        before = settings_service.get_safety_thresholds()

    response = operator_client.post(
        "/settings/safety-thresholds",
        data={**VALID_THRESHOLDS, "wind_safe_max_kmh": "1"},
    )
    assert response.status_code == 403

    with app.app_context():
        after = settings_service.get_safety_thresholds()
        assert before == after


# ---------------------------------------------------------------------------
# Refresh Interval
# ---------------------------------------------------------------------------

def test_current_interval_loads(app):
    admin_client, _ = _admin(app)
    with app.app_context():
        expected = settings_service.get_refresh_interval_seconds()
    response = admin_client.get("/settings")
    assert f"Current interval: {expected} seconds".encode() in response.data


def test_valid_interval_can_be_updated(app):
    admin_client, _ = _admin(app)
    response = admin_client.post(
        "/settings/refresh-interval",
        data={"refresh_interval_seconds": "30"},
        follow_redirects=True,
    )
    assert b"Refresh interval set to 30 seconds" in response.data
    with app.app_context():
        assert settings_service.get_refresh_interval_seconds() == 30


def test_invalid_interval_rejected(app):
    admin_client, _ = _admin(app)
    with app.app_context():
        before = settings_service.get_refresh_interval_seconds()

    response = admin_client.post(
        "/settings/refresh-interval",
        data={"refresh_interval_seconds": "15"},
        follow_redirects=True,
    )
    assert b"Refresh interval must be one of" in response.data

    with app.app_context():
        assert settings_service.get_refresh_interval_seconds() == before


def test_environmental_auto_refresh_uses_configured_interval(app):
    admin_client, _ = _admin(app)
    admin_client.post("/settings/refresh-interval", data={"refresh_interval_seconds": "30"})

    response = admin_client.get("/monitoring")
    assert response.status_code == 200
    assert b'Number("30")' in response.data

    response = admin_client.get("/dashboard")
    assert response.status_code == 200
    assert b'data-refresh-interval="30"' in response.data


# ---------------------------------------------------------------------------
# Security & Activity
# ---------------------------------------------------------------------------

def test_activity_records_created_for_supported_actions(app, make_boat, make_trip):
    admin_client, admin_id = _admin(app)

    # Threshold change
    admin_client.post("/settings/safety-thresholds", data=VALID_THRESHOLDS)

    # Passenger approval / rejection
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    with app.app_context():
        reg1 = PendingRegistration(
            trip_id=trip_id, full_name="Alice", age=30, address="Addr",
            contact_number="0900000000", passenger_type="Regular",
        )
        reg2 = PendingRegistration(
            trip_id=trip_id, full_name="Bob", age=40, address="Addr",
            contact_number="0900000001", passenger_type="Regular",
        )
        db.session.add_all([reg1, reg2])
        db.session.commit()
        reg1_id, reg2_id = reg1.id, reg2.id

    admin_client.post(f"/manifests/{trip_id}/pending/{reg1_id}/approve")
    admin_client.post(f"/manifests/{trip_id}/pending/{reg2_id}/reject")

    with app.app_context():
        actions = {log.action for log in ActivityLog.query.all()}
        assert "Updated Safety Thresholds" in actions
        assert "Passenger approval" in actions
        assert "Passenger rejection" in actions


def test_admin_can_access_activity_log(app):
    admin_client, _ = _admin(app)
    response = admin_client.get("/settings/activity-log")
    assert response.status_code == 200


def test_unauthorized_cannot_access_activity_log(app, client):
    response = client.get("/settings/activity-log")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_operator_cannot_access_activity_log(app):
    operator_client, _ = _operator(app)
    response = operator_client.get("/settings/activity-log")
    assert response.status_code == 403


def test_sensitive_credentials_are_not_recorded(app):
    admin_client, _ = _admin(app)
    admin_client.post(
        "/settings/change-password",
        data={
            "current_password": "StartPass123",
            "new_password": "BrandNewPass456",
            "confirm_password": "BrandNewPass456",
        },
    )

    with app.app_context():
        for log in ActivityLog.query.all():
            haystack = f"{log.action} {log.details or ''}"
            assert "StartPass123" not in haystack
            assert "BrandNewPass456" not in haystack
