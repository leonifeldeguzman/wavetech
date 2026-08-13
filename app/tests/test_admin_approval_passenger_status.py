"""
Test suite for Task 3: Admin Approval -> Passenger Portal status.

Covers:
    GET /api/passenger/registrations/<reference_code>

and its interaction with the EXISTING (unmodified) admin approval
routes in app/blueprints/manifests/routes.py:
    POST /manifests/<trip_id>/pending/<reg_id>/approve
    POST /manifests/<trip_id>/pending/<reg_id>/reject

Run with:
    pytest tests/test_admin_approval_passenger_status.py -v
"""
import json

from app.extensions import db
from app.models.pending_registration import PendingRegistration
from app.models.manifest_entry import ManifestEntry


def _register_passenger(client, trip_id, full_name="Test Passenger"):
    """Creates a real pending registration via the actual passenger API
    (not a direct DB insert), so these tests exercise the full stack."""
    resp = client.post(
        "/api/passenger/registrations",
        data=json.dumps({
            "trip_id": trip_id,
            "full_name": full_name,
            "age": 25,
            "address": "Test Address",
            "contact_number": "09123456789",
            "passenger_type": "Regular",
        }),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["reference_code"]


# ---------------------------------------------------------------------------
# Pending status
# ---------------------------------------------------------------------------

def test_status_pending_before_admin_action(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reference_code = _register_passenger(client, trip_id)

    resp = client.get(f"/api/passenger/registrations/{reference_code}")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["data"]["reference_code"] == reference_code
    assert body["data"]["trip_id"] == trip_id
    assert body["data"]["status"] == "pending"
    assert body["data"]["full_name"] == "Test Passenger"


# ---------------------------------------------------------------------------
# Approved status (via the EXISTING admin approval route)
# ---------------------------------------------------------------------------

def test_status_approved_after_admin_approval(client, admin_client, make_boat, make_trip, app):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reference_code = _register_passenger(client, trip_id)

    with app.app_context():
        reg = PendingRegistration.query.filter_by(reference_code=reference_code).first()
        reg_id = reg.id

    approve_resp = admin_client.post(
        f"/manifests/{trip_id}/pending/{reg_id}/approve",
        data={},
        follow_redirects=False,
    )
    # Existing admin route redirects back to trip_detail on success.
    assert approve_resp.status_code == 302

    status_resp = client.get(f"/api/passenger/registrations/{reference_code}")
    body = status_resp.get_json()

    assert status_resp.status_code == 200
    assert body["data"]["status"] == "approved"


# ---------------------------------------------------------------------------
# Rejected status (via the EXISTING admin rejection route)
# ---------------------------------------------------------------------------

def test_status_rejected_after_admin_rejection(client, admin_client, make_boat, make_trip, app):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reference_code = _register_passenger(client, trip_id)

    with app.app_context():
        reg = PendingRegistration.query.filter_by(reference_code=reference_code).first()
        reg_id = reg.id

    reject_resp = admin_client.post(
        f"/manifests/{trip_id}/pending/{reg_id}/reject",
        follow_redirects=False,
    )
    assert reject_resp.status_code == 302

    status_resp = client.get(f"/api/passenger/registrations/{reference_code}")
    body = status_resp.get_json()

    assert status_resp.status_code == 200
    assert body["data"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# Invalid reference code
# ---------------------------------------------------------------------------

def test_status_invalid_reference_code_returns_404(client):
    resp = client.get("/api/passenger/registrations/WT-2026-000000")
    body = resp.get_json()

    assert resp.status_code == 404
    assert body["success"] is False
    assert body["error"]["code"] == "REGISTRATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# No double counting after a REAL admin approval (end-to-end, through the
# actual routes rather than direct DB inserts)
# ---------------------------------------------------------------------------

def test_approved_registration_not_double_counted_end_to_end(
    client, admin_client, make_boat, make_trip, app
):
    boat_id = make_boat(capacity=2)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    reference_code = _register_passenger(client, trip_id, full_name="Passenger A")

    with app.app_context():
        reg = PendingRegistration.query.filter_by(reference_code=reference_code).first()
        reg_id = reg.id

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve", data={})

    # Sanity check on the existing admin-side data: exactly one
    # ManifestEntry, and the PendingRegistration row still exists with
    # status="approved" (not deleted) — this is the existing behavior
    # this feature depends on, unmodified.
    with app.app_context():
        assert ManifestEntry.query.filter_by(trip_id=trip_id).count() == 1
        still_pending_row = PendingRegistration.query.filter_by(
            reference_code=reference_code
        ).first()
        assert still_pending_row.status == "approved"

    trip_resp = client.get(f"/api/trips/{trip_id}")
    trip_data = trip_resp.get_json()["data"]

    # Capacity is 2; only 1 seat should be reported occupied, not 2
    # (which would happen if both the ManifestEntry AND the now-approved
    # PendingRegistration were counted).
    assert trip_data["current_passenger_count"] == 1
    assert trip_data["remaining_capacity"] == 1


# ---------------------------------------------------------------------------
# Regression: existing admin approval behavior is unaffected
# ---------------------------------------------------------------------------

def test_existing_admin_approval_still_creates_manifest_entry(
    client, admin_client, make_boat, make_trip, app
):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reference_code = _register_passenger(client, trip_id, full_name="Regression Passenger")

    with app.app_context():
        reg = PendingRegistration.query.filter_by(reference_code=reference_code).first()
        reg_id = reg.id

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve", data={})

    with app.app_context():
        entry = ManifestEntry.query.filter_by(trip_id=trip_id, source="online").first()
        assert entry is not None
        assert entry.full_name == "Regression Passenger"


def test_existing_admin_reject_still_works_without_manifest_entry(
    client, admin_client, make_boat, make_trip, app
):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    reference_code = _register_passenger(client, trip_id, full_name="Rejected Passenger")

    with app.app_context():
        reg = PendingRegistration.query.filter_by(reference_code=reference_code).first()
        reg_id = reg.id

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/reject")

    with app.app_context():
        entry = ManifestEntry.query.filter_by(trip_id=trip_id, full_name="Rejected Passenger").first()
        assert entry is None