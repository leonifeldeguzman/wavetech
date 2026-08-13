"""
Test suite for the Passenger Portal backend (Phase 1):
    GET  /api/trips
    GET  /api/trips/<trip_id>
    POST /api/passenger/registrations

Covers the scenarios listed in the task's Step 11 and Step 12.

Run with (from repo root, inside your normal project virtualenv where
requirements.txt is installed):

    pip install pytest
    python -m pytest app/tests/test_passenger_portal.py -v
"""
import json
import threading

import pytest

from app.extensions import db
from app.models.pending_registration import PendingRegistration
from app.models.manifest_entry import ManifestEntry
from app.models.trip import Trip


def _post_registration(client, **overrides):
    payload = {
        "trip_id": 1,
        "full_name": "Test Passenger",
        "age": 21,
        "address": "Test Address",
        "contact_number": "09123456789",
        "passenger_type": "Regular",
    }
    payload.update(overrides)
    return client.post(
        "/api/passenger/registrations",
        data=json.dumps(payload),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# TEST 1 / Step 2 — GET /api/trips
# ---------------------------------------------------------------------------

def test_list_trips_returns_selectable_trips(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    open_trip = make_trip(boat_id=boat_id, status="Open")
    cancelled_trip = make_trip(boat_id=boat_id, status="Cancelled")

    resp = client.get("/api/trips")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["success"] is True
    trip_ids = [t["trip_id"] for t in body["data"]]
    assert open_trip in trip_ids
    assert cancelled_trip not in trip_ids  # Cancelled must not be listed


def test_list_trips_excludes_departed_and_full(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    departed_trip = make_trip(boat_id=boat_id, status="Departed")
    full_trip = make_trip(boat_id=boat_id, status="Full")
    open_trip = make_trip(boat_id=boat_id, status="Open")

    resp = client.get("/api/trips")
    trip_ids = [t["trip_id"] for t in resp.get_json()["data"]]

    assert departed_trip not in trip_ids
    assert full_trip not in trip_ids
    assert open_trip in trip_ids


# ---------------------------------------------------------------------------
# TEST 2 / Step 3 — GET /api/trips/<trip_id>
# ---------------------------------------------------------------------------

def test_get_trip_detail_success(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    resp = client.get(f"/api/trips/{trip_id}")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["data"]["trip_id"] == trip_id
    assert body["data"]["capacity"] == 5
    assert body["data"]["current_passenger_count"] == 0
    assert body["data"]["remaining_capacity"] == 5
    assert body["data"]["accepts_registration"] is True


def test_get_trip_detail_invalid_id_returns_404(client):
    resp = client.get("/api/trips/999999")
    body = resp.get_json()

    assert resp.status_code == 404
    assert body["success"] is False
    assert body["error"]["code"] == "TRIP_NOT_FOUND"


# ---------------------------------------------------------------------------
# TEST 3 — POST /api/passenger/registrations (success)
# ---------------------------------------------------------------------------

def test_successful_registration(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    resp = _post_registration(client, trip_id=trip_id)
    body = resp.get_json()

    assert resp.status_code == 201
    assert body["success"] is True
    assert body["data"]["trip_id"] == trip_id
    assert body["data"]["status"] == "pending"
    assert body["data"]["reference_code"].startswith("WT-2026-")

    reg = PendingRegistration.query.filter_by(
        reference_code=body["data"]["reference_code"]
    ).first()
    assert reg is not None
    assert reg.status == "pending"
    assert reg.trip_id == trip_id


# ---------------------------------------------------------------------------
# TEST 4 / Step 5 — capacity reflected after registration
# ---------------------------------------------------------------------------

def test_capacity_reflects_new_pending_registration(client, make_boat, make_trip):
    boat_id = make_boat(capacity=3)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    _post_registration(client, trip_id=trip_id, full_name="Passenger A")

    resp = client.get(f"/api/trips/{trip_id}")
    data = resp.get_json()["data"]

    assert data["current_passenger_count"] == 1
    assert data["remaining_capacity"] == 2


def test_approved_pending_registration_not_double_counted(client, make_boat, make_trip, app):
    """
    Regression test for the double-counting ambiguity: once a
    PendingRegistration is approved by the existing admin flow, it stays
    in pending_registrations with status='approved' AND a matching
    ManifestEntry is created. Reserved seats must count that as ONE
    occupied seat, not two.
    """
    boat_id = make_boat(capacity=3)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id, full_name="Already Approved", age=30, status="approved"
        )
        db.session.add(reg)
        db.session.flush()
        entry = ManifestEntry(
            trip_id=trip_id, full_name="Already Approved", age=30, source="online"
        )
        db.session.add(entry)
        db.session.commit()

    resp = client.get(f"/api/trips/{trip_id}")
    data = resp.get_json()["data"]

    # Only 1 occupied seat (the ManifestEntry), not 2.
    assert data["current_passenger_count"] == 1
    assert data["remaining_capacity"] == 2


# ---------------------------------------------------------------------------
# TEST 5 / Step 11 — validation errors
# ---------------------------------------------------------------------------

def test_missing_trip_id(client):
    resp = _post_registration(client, trip_id=None)
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "trip_id" in body["error"]["fields"]


def test_missing_full_name(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id)
    resp = _post_registration(client, trip_id=trip_id, full_name="")
    body = resp.get_json()
    assert resp.status_code == 400
    assert "full_name" in body["error"]["fields"]


def test_missing_age(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id)
    resp = _post_registration(client, trip_id=trip_id, age=None)
    body = resp.get_json()
    assert resp.status_code == 400
    assert "age" in body["error"]["fields"]


def test_invalid_age(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id)
    resp = _post_registration(client, trip_id=trip_id, age=-5)
    body = resp.get_json()
    assert resp.status_code == 400
    assert "age" in body["error"]["fields"]


def test_invalid_passenger_type(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id)
    resp = _post_registration(client, trip_id=trip_id, passenger_type="Astronaut")
    body = resp.get_json()
    assert resp.status_code == 400
    assert "passenger_type" in body["error"]["fields"]


def test_invalid_trip_id_on_registration(client):
    resp = _post_registration(client, trip_id=999999)
    body = resp.get_json()
    assert resp.status_code == 404
    assert body["error"]["code"] == "TRIP_NOT_FOUND"


# ---------------------------------------------------------------------------
# TEST 6 — cancelled trip
# ---------------------------------------------------------------------------

def test_registration_rejected_for_cancelled_trip(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id, status="Cancelled")

    resp = _post_registration(client, trip_id=trip_id)
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error"]["code"] == "TRIP_NOT_AVAILABLE"
    assert PendingRegistration.query.filter_by(trip_id=trip_id).count() == 0


def test_registration_rejected_for_departed_trip(client, make_boat, make_trip):
    boat_id = make_boat()
    trip_id = make_trip(boat_id=boat_id, status="Departed")

    resp = _post_registration(client, trip_id=trip_id)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "TRIP_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# TEST 7 — full trip
# ---------------------------------------------------------------------------

def test_registration_rejected_when_full(client, make_boat, make_trip):
    boat_id = make_boat(capacity=1)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    first = _post_registration(client, trip_id=trip_id, full_name="First Passenger")
    assert first.status_code == 201

    second = _post_registration(client, trip_id=trip_id, full_name="Second Passenger")
    body = second.get_json()

    assert second.status_code == 409
    assert body["error"]["code"] == "TRIP_FULL"
    assert PendingRegistration.query.filter_by(trip_id=trip_id).count() == 1

    trip = Trip.query.get(trip_id)
    assert trip.status == "Full"


def test_registration_rejected_no_boat_assigned(client, make_trip):
    trip_id = make_trip(boat_id=None, status="Open")
    resp = _post_registration(client, trip_id=trip_id)
    body = resp.get_json()
    assert resp.status_code == 409
    assert body["error"]["code"] == "TRIP_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# Reference code uniqueness
# ---------------------------------------------------------------------------

def test_reference_codes_are_unique(client, make_boat, make_trip):
    boat_id = make_boat(capacity=10)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    codes = set()
    for i in range(5):
        resp = _post_registration(client, trip_id=trip_id, full_name=f"Passenger {i}")
        assert resp.status_code == 201
        codes.add(resp.get_json()["data"]["reference_code"])

    assert len(codes) == 5


# ---------------------------------------------------------------------------
# Concurrency — Step 12 Test scenario: only one seat remains
# ---------------------------------------------------------------------------

def test_concurrent_registration_last_seat(app, make_boat, make_trip):
    """
    SELECT...FOR UPDATE is a documented no-op on dialects that don't
    support it — SQLite among them — so this test only proves anything
    when run against Postgres. On SQLite the lock never actually
    engages and both concurrent requests will incorrectly succeed; that
    is expected and this test skips itself in that case rather than
    reporting a false failure.

    To actually verify the concurrency guarantee, point DATABASE_URL at
    a real Postgres instance (e.g. the same one configured in your .env)
    before running this specific test:

        DATABASE_URL=postgresql://user:pass@localhost/wavetech_test \
            pytest tests/test_passenger_portal.py::test_concurrent_registration_last_seat -v
    """
    with app.app_context():
        dialect = db.engine.dialect.name
    if dialect != "postgresql":
        pytest.skip(
            f"SELECT...FOR UPDATE is a no-op on the '{dialect}' dialect; "
            "this test cannot validate row-locking here. Re-run against "
            "a Postgres DATABASE_URL to verify the concurrency guarantee."
        )

    boat_id = make_boat(capacity=1)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    results = []

    def attempt(name):
        with app.test_client() as c:
            resp = _post_registration(c, trip_id=trip_id, full_name=name)
            results.append(resp.status_code)

    t1 = threading.Thread(target=attempt, args=("Racer A",))
    t2 = threading.Thread(target=attempt, args=("Racer B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    with app.app_context():
        successful = PendingRegistration.query.filter_by(trip_id=trip_id).count()

    assert successful == 1
    assert results.count(201) == 1
    assert results.count(409) == 1