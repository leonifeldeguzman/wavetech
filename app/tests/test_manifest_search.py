"""Tests for the search feature on Pending Registrations and Passenger
Manifest (both live on GET /manifests/<trip_id>, app/blueprints/manifests/
routes.py::trip_detail).

Search is server-side, via ?pending_q=... and ?manifest_q=... query
params, filtered with SQLAlchemy's ilike() (case-insensitive, partial,
parameterized) on top of the SAME trip-scoped queries that already
existed — so authorization/scoping is inherited rather than reimplemented.

Run with:
    python -m pytest app/tests/test_manifest_search.py -v
"""
from app.extensions import db
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration


def _make_pending(app, trip_id, full_name, reference_code, status="pending"):
    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id,
            full_name=full_name,
            age=30,
            address="Test Address",
            contact_number="09123456789",
            passenger_type="Regular",
            reference_code=reference_code,
            status=status,
        )
        db.session.add(reg)
        db.session.commit()
        return reg.id


def _make_manifest_entry(app, trip_id, full_name, passenger_type="Regular", source="walkin"):
    with app.app_context():
        entry = ManifestEntry(
            trip_id=trip_id,
            full_name=full_name,
            age=30,
            address="Test Address",
            contact_number="09123456789",
            passenger_type=passenger_type,
            source=source,
        )
        db.session.add(entry)
        db.session.commit()
        return entry.id


def _client_with_role(app, role):
    """login_required (app/utils/decorators.py) only checks
    session["user_id"] — mirrors the existing `admin_client` fixture in
    conftest.py, just with an explicit role so both Admin and Operator
    can be exercised in the same test file."""
    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["full_name"] = f"Test {role}"
        sess["role"] = role
    return test_client


def _detail_url(trip_id, **params):
    from urllib.parse import urlencode

    base = f"/manifests/{trip_id}"
    return f"{base}?{urlencode(params)}" if params else base


# ---------------------------------------------------------------------------
# Pending Registrations search
# ---------------------------------------------------------------------------

def test_search_pending_by_exact_reference_code(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-123456")
    _make_pending(app, trip_id, "Maria Santos", "WT-2026-654321")

    response = admin_client.get(_detail_url(trip_id, pending_q="WT-2026-123456"))

    assert response.status_code == 200
    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_pending_by_partial_reference_code(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-123456")
    _make_pending(app, trip_id, "Maria Santos", "WT-2026-654321")

    response = admin_client.get(_detail_url(trip_id, pending_q="123456"))

    assert response.status_code == 200
    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_pending_by_first_name(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")
    _make_pending(app, trip_id, "Maria Santos", "WT-2026-222222")

    response = admin_client.get(_detail_url(trip_id, pending_q="Juan"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_pending_by_last_name(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")
    _make_pending(app, trip_id, "Maria Santos", "WT-2026-222222")

    response = admin_client.get(_detail_url(trip_id, pending_q="Cruz"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_pending_by_full_name(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")
    _make_pending(app, trip_id, "Maria Santos", "WT-2026-222222")

    response = admin_client.get(_detail_url(trip_id, pending_q="Juan Dela Cruz"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_pending_case_insensitive(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")

    response = admin_client.get(_detail_url(trip_id, pending_q="juan dela cruz"))
    assert b"Juan Dela Cruz" in response.data

    response = admin_client.get(_detail_url(trip_id, pending_q="wt-2026-111111"))
    assert b"Juan Dela Cruz" in response.data


def test_search_pending_no_results_shows_empty_state(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")

    response = admin_client.get(_detail_url(trip_id, pending_q="Nonexistent Passenger"))

    assert response.status_code == 200
    assert b"Juan Dela Cruz" not in response.data
    assert b"No pending registrations match" in response.data


def test_search_pending_works_for_admin_and_operator(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")

    admin = _client_with_role(app, "Admin")
    operator = _client_with_role(app, "Operator")

    admin_resp = admin.get(_detail_url(trip_id, pending_q="Juan"))
    operator_resp = operator.get(_detail_url(trip_id, pending_q="Juan"))

    assert admin_resp.status_code == 200
    assert operator_resp.status_code == 200
    assert b"Juan Dela Cruz" in admin_resp.data
    assert b"Juan Dela Cruz" in operator_resp.data


def test_pending_search_excludes_already_approved_or_rejected(app, admin_client, make_boat, make_trip):
    """Search must not defeat the EXISTING status="pending" scoping —
    approved/rejected registrations should stay excluded, search term or
    not."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_id, "Juan Approved", "WT-2026-111111", status="approved")
    _make_pending(app, trip_id, "Juan Pending", "WT-2026-222222", status="pending")

    response = admin_client.get(_detail_url(trip_id, pending_q="Juan"))

    assert b"Juan Pending" in response.data
    assert b"Juan Approved" not in response.data


def test_pending_search_scoped_to_trip_does_not_leak_other_trips(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_a = make_trip(boat_id=boat_id, status="Open")
    trip_b = make_trip(boat_id=boat_id, status="Open")
    _make_pending(app, trip_a, "Juan Dela Cruz", "WT-2026-111111")
    _make_pending(app, trip_b, "Juan Reyes", "WT-2026-222222")

    response = admin_client.get(_detail_url(trip_a, pending_q="Juan"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Juan Reyes" not in response.data


def test_pending_approve_still_works_with_search_active(app, admin_client, make_boat, make_trip):
    """Searching must not change the existing approve/reject actions."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reg_id = _make_pending(app, trip_id, "Juan Dela Cruz", "WT-2026-111111")

    response = admin_client.get(_detail_url(trip_id, pending_q="Juan"))
    assert response.status_code == 200

    approve_resp = admin_client.post(
        f"/manifests/{trip_id}/pending/{reg_id}/approve", data={}
    )
    assert approve_resp.status_code == 302

    with app.app_context():
        reg = db.session.get(PendingRegistration, reg_id)
        assert reg.status == "approved"
        assert ManifestEntry.query.filter_by(trip_id=trip_id).count() == 1


# ---------------------------------------------------------------------------
# Passenger Manifest search
# ---------------------------------------------------------------------------

def test_search_manifest_by_name(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")
    _make_manifest_entry(app, trip_id, "Maria Santos", passenger_type="Student")

    response = admin_client.get(_detail_url(trip_id, manifest_q="Juan Dela Cruz"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_manifest_by_partial_name(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")
    _make_manifest_entry(app, trip_id, "Maria Santos", passenger_type="Student")

    response = admin_client.get(_detail_url(trip_id, manifest_q="Cruz"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data


def test_search_manifest_by_passenger_type(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")
    _make_manifest_entry(app, trip_id, "Maria Santos", passenger_type="Senior")
    _make_manifest_entry(app, trip_id, "Pedro Reyes", passenger_type="Student")

    response = admin_client.get(_detail_url(trip_id, manifest_q="Senior"))

    assert b"Maria Santos" in response.data
    assert b"Juan Dela Cruz" not in response.data
    assert b"Pedro Reyes" not in response.data


def test_search_manifest_case_insensitive(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")

    response = admin_client.get(_detail_url(trip_id, manifest_q="juan dela cruz"))
    assert b"Juan Dela Cruz" in response.data

    response = admin_client.get(_detail_url(trip_id, manifest_q="regular"))
    assert b"Juan Dela Cruz" in response.data


def test_search_manifest_no_results_shows_empty_state(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")

    response = admin_client.get(_detail_url(trip_id, manifest_q="Nonexistent"))

    assert response.status_code == 200
    assert b"Juan Dela Cruz" not in response.data
    assert b"No passengers match" in response.data


def test_manifest_search_works_for_admin_and_operator(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")

    admin = _client_with_role(app, "Admin")
    operator = _client_with_role(app, "Operator")

    assert b"Juan Dela Cruz" in admin.get(_detail_url(trip_id, manifest_q="Juan")).data
    assert b"Juan Dela Cruz" in operator.get(_detail_url(trip_id, manifest_q="Juan")).data


def test_manifest_search_does_not_leak_other_trip_entries(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_a = make_trip(boat_id=boat_id, status="Open")
    trip_b = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_a, "Juan Dela Cruz")
    _make_manifest_entry(app, trip_b, "Juan Reyes")

    response = admin_client.get(_detail_url(trip_a, manifest_q="Juan"))

    assert b"Juan Dela Cruz" in response.data
    assert b"Juan Reyes" not in response.data


def test_manifest_search_does_not_affect_capacity_count(app, admin_client, make_boat, make_trip):
    """Regression guard: the capacity/occupancy indicator must reflect
    the TRUE manifest, not the filtered/searched subset."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")
    _make_manifest_entry(app, trip_id, "Maria Santos", passenger_type="Student")
    _make_manifest_entry(app, trip_id, "Pedro Reyes", passenger_type="Senior")

    response = admin_client.get(_detail_url(trip_id, manifest_q="Juan"))

    assert response.status_code == 200
    # Only Juan's row is shown...
    assert b"Juan Dela Cruz" in response.data
    assert b"Maria Santos" not in response.data
    # ...but the capacity indicator still reports the true count of 3.
    assert b"3/5 Capacity" in response.data


def test_manifest_add_walkin_still_works_with_search_active(app, admin_client, make_boat, make_trip):
    """Searching must not change the existing add-walk-in action."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, "Juan Dela Cruz", passenger_type="Regular")

    response = admin_client.get(_detail_url(trip_id, manifest_q="nonexistent"))
    assert response.status_code == 200

    walkin_resp = admin_client.post(
        f"/manifests/{trip_id}/add-walkin",
        data={
            "full_name": "Walk-In Passenger",
            "age": "40",
            "address": "Somewhere",
            "contact_number": "09123456789",
            "passenger_type": "Regular",
        },
    )
    assert walkin_resp.status_code == 302

    with app.app_context():
        assert ManifestEntry.query.filter_by(
            trip_id=trip_id, full_name="Walk-In Passenger"
        ).count() == 1