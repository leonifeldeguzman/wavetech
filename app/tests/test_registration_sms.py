"""
Tests for the registration-confirmation SMS hook added in
_execute_registration() (Phase 6B). These deliberately never hit the
real Semaphore API — semaphore_service.send_sms is monkeypatched in
every test here.

Key guarantee under test: nothing about SMS sending/logging may change
the HTTP result of a registration that already committed successfully.
That's why several tests below make the fake send_sms behave badly
(return a failure, raise an exception) and still assert a 201.
"""
from app.extensions import db
from app.models.activity_log import ActivityLog
from app.services.activity_log_service import ACTION_REGISTRATION_SMS_SENT
from app.services.semaphore_service import SmsResult


def _valid_payload(trip_id, **overrides):
    payload = {
        "trip_id": trip_id,
        "full_name": "Juan Dela Cruz",
        "age": 30,
        "address": "Talim Island",
        "contact_number": "09171234567",
        "passenger_type": "Regular",
    }
    payload.update(overrides)
    return payload


def test_registration_sends_sms_when_contact_number_present(client, app, make_boat, make_trip, monkeypatch):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id)

    calls = []

    def fake_send_sms(number, message):
        calls.append((number, message))
        return SmsResult(success=True, status="sent", raw_response={"ok": True})

    monkeypatch.setattr("app.services.semaphore_service.send_sms", fake_send_sms)

    response = client.post("/api/passenger/registrations", json=_valid_payload(trip_id))
    assert response.status_code == 201

    reference_code = response.get_json()["data"]["reference_code"]

    # send_sms was called with the passenger's number and a message that
    # includes the reference code, so the passenger can cross-check it.
    assert len(calls) == 1
    number, message = calls[0]
    assert number == "09171234567"
    assert reference_code in message

    with app.app_context():
        logs = ActivityLog.query.filter_by(action=ACTION_REGISTRATION_SMS_SENT).all()
        assert len(logs) == 1
        assert logs[0].user_id is None
        assert logs[0].admin_name == "System"
        assert reference_code in logs[0].details
        assert "status=sent" in logs[0].details


def test_registration_skips_log_when_no_contact_number(client, app, make_boat, make_trip, monkeypatch):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id)

    def fake_send_sms(number, message):
        # Mirrors the real semaphore_service behavior for a falsy number.
        return SmsResult(success=False, status="skipped_no_number")

    monkeypatch.setattr("app.services.semaphore_service.send_sms", fake_send_sms)

    response = client.post(
        "/api/passenger/registrations",
        json=_valid_payload(trip_id, contact_number=""),
    )
    assert response.status_code == 201

    # A passenger not supplying a contact number is routine, not an
    # event worth an audit log entry (see _send_registration_confirmation_sms).
    with app.app_context():
        logs = ActivityLog.query.filter_by(action=ACTION_REGISTRATION_SMS_SENT).all()
        assert logs == []


def test_registration_still_succeeds_when_sms_fails(client, app, make_boat, make_trip, monkeypatch):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id)

    def fake_send_sms(number, message):
        return SmsResult(success=False, status="failed", error="simulated network error")

    monkeypatch.setattr("app.services.semaphore_service.send_sms", fake_send_sms)

    response = client.post("/api/passenger/registrations", json=_valid_payload(trip_id))

    # The registration itself must still be reported as successful — an
    # SMS delivery failure is not a registration failure.
    assert response.status_code == 201
    assert response.get_json()["data"]["reference_code"]

    with app.app_context():
        logs = ActivityLog.query.filter_by(action=ACTION_REGISTRATION_SMS_SENT).all()
        assert len(logs) == 1
        assert "status=failed" in logs[0].details
        assert "simulated network error" in logs[0].details


def test_registration_still_succeeds_when_sms_raises_unexpectedly(client, app, make_boat, make_trip, monkeypatch):
    def fake_send_sms(number, message):
        raise RuntimeError("boom — should never propagate to the caller")

    monkeypatch.setattr("app.services.semaphore_service.send_sms", fake_send_sms)

    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id)

    response = client.post("/api/passenger/registrations", json=_valid_payload(trip_id))

    # Even an unexpected exception inside the SMS/logging helper must be
    # swallowed — the registration already committed and must still be
    # reported as a success.
    assert response.status_code == 201

    with app.app_context():
        # No activity log entry either, since the log_action() call never
        # ran (the exception happened before reaching it, in this fake).
        logs = ActivityLog.query.filter_by(action=ACTION_REGISTRATION_SMS_SENT).all()
        assert logs == []


def test_registration_via_html_form_also_sends_sms(client, app, make_boat, make_trip, monkeypatch):
    """The HTML form route shares _execute_registration() with the JSON
    API route, so it should get the same SMS behavior for free."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id)

    calls = []

    def fake_send_sms(number, message):
        calls.append((number, message))
        return SmsResult(success=True, status="sent")

    monkeypatch.setattr("app.services.semaphore_service.send_sms", fake_send_sms)

    response = client.post(
        f"/passenger/trips/{trip_id}/register",
        data=_valid_payload(trip_id),
        follow_redirects=False,
    )
    assert response.status_code == 302  # redirect to registration_status_page
    assert len(calls) == 1