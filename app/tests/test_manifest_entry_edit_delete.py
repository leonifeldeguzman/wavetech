"""Tests for the Edit/Delete actions on the Passenger Manifest table
(app/blueprints/manifests/routes.py::edit_manifest_entry /
delete_manifest_entry), available to both Admin and Operator.

Run with:
    python -m pytest app/tests/test_manifest_entry_edit_delete.py -v
"""
from app.extensions import db
from app.models.activity_log import ActivityLog
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration


def _make_manifest_entry(app, trip_id, full_name="Juan Dela Cruz", age=30,
                          address="Test Address", contact_number="09123456789",
                          passenger_type="Regular", source="walkin"):
    with app.app_context():
        entry = ManifestEntry(
            trip_id=trip_id,
            full_name=full_name,
            age=age,
            address=address,
            contact_number=contact_number,
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
    can be exercised directly."""
    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["full_name"] = f"Test {role}"
        sess["role"] = role
    return test_client


def _edit_payload(**overrides):
    payload = {
        "full_name": "Juan Dela Cruz Updated",
        "age": "31",
        "address": "New Address",
        "contact_number": "09987654321",
        "passenger_type": "Senior",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Edit — Admin and Operator
# ---------------------------------------------------------------------------

def test_admin_can_edit_manifest_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    response = admin.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/edit",
        data=_edit_payload(),
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.full_name == "Juan Dela Cruz Updated"
        assert entry.age == 31
        assert entry.address == "New Address"
        assert entry.contact_number == "09987654321"
        assert entry.passenger_type == "Senior"


def test_operator_can_edit_manifest_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    operator = _client_with_role(app, "Operator")

    response = operator.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/edit",
        data=_edit_payload(full_name="Operator Edited Name"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.full_name == "Operator Edited Name"


def test_edit_does_not_create_duplicate_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/edit", data=_edit_payload())

    with app.app_context():
        assert ManifestEntry.query.filter_by(trip_id=trip_id).count() == 1
        assert db.session.get(ManifestEntry, entry_id) is not None


def test_edit_preserves_non_editable_fields(app, make_boat, make_trip):
    """id, trip_id, source, and check_in_time must never change from an
    edit — only the passenger's own details are editable."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id, source="online")
    admin = _client_with_role(app, "Admin")

    with app.app_context():
        original = db.session.get(ManifestEntry, entry_id)
        original_check_in = original.check_in_time
        original_source = original.source
        original_trip_id = original.trip_id

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/edit", data=_edit_payload())

    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.id == entry_id
        assert entry.trip_id == original_trip_id
        assert entry.source == original_source
        assert entry.check_in_time == original_check_in


def test_edit_rejects_invalid_age(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id, age=30)
    admin = _client_with_role(app, "Admin")

    response = admin.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/edit",
        data=_edit_payload(age="not-a-number"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        # Unchanged — the invalid update was rejected, not partially applied.
        assert entry.age == 30
        assert entry.full_name == "Juan Dela Cruz"


def test_edit_rejects_invalid_passenger_type(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id, passenger_type="Regular")
    admin = _client_with_role(app, "Admin")

    admin.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/edit",
        data=_edit_payload(passenger_type="NotARealType"),
    )

    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.passenger_type == "Regular"


def test_edit_keeps_associated_pending_registration_status_unchanged(app, make_boat, make_trip):
    """Editing a manifest entry that came from an approved online
    registration must not touch that registration's status."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id,
            full_name="Juan Dela Cruz",
            age=30,
            passenger_type="Regular",
            status="approved",
        )
        db.session.add(reg)
        db.session.commit()
        reg_id = reg.id

    entry_id = _make_manifest_entry(app, trip_id, source="online")
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/edit", data=_edit_payload())

    with app.app_context():
        reg = db.session.get(PendingRegistration, reg_id)
        assert reg.status == "approved"


def test_edit_activity_log_recorded(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/edit", data=_edit_payload())

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Edited Passenger Manifest").all()
        assert len(logs) == 1
        assert "Juan Dela Cruz" in logs[0].details
        assert logs[0].admin_name == "Test Admin"
        assert logs[0].created_at is not None


def test_edit_cannot_target_entry_from_different_trip(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_a = make_trip(boat_id=boat_id, status="Open")
    trip_b = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_b)
    admin = _client_with_role(app, "Admin")

    response = admin.post(
        f"/manifests/{trip_a}/manifest/{entry_id}/edit", data=_edit_payload()
    )
    assert response.status_code == 404

    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.full_name == "Juan Dela Cruz"  # untouched


def test_unauthenticated_cannot_edit_manifest_entry(app, client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)

    response = client.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/edit",
        data=_edit_payload(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/login" in response.headers["Location"] or "login" in response.headers["Location"]
    with app.app_context():
        entry = db.session.get(ManifestEntry, entry_id)
        assert entry.full_name == "Juan Dela Cruz"


def test_edit_manifest_entry_requires_post(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    response = admin.get(f"/manifests/{trip_id}/manifest/{entry_id}/edit")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Delete — Admin and Operator
# ---------------------------------------------------------------------------

def test_admin_can_delete_manifest_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    response = admin.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(ManifestEntry, entry_id) is None


def test_operator_can_delete_manifest_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    operator = _client_with_role(app, "Operator")

    response = operator.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/delete", follow_redirects=True
    )

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(ManifestEntry, entry_id) is None


def test_delete_removes_only_selected_entry(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    keep_id = _make_manifest_entry(app, trip_id, full_name="Keep Me")
    delete_id = _make_manifest_entry(app, trip_id, full_name="Delete Me")
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{delete_id}/delete")

    with app.app_context():
        assert db.session.get(ManifestEntry, delete_id) is None
        remaining = db.session.get(ManifestEntry, keep_id)
        assert remaining is not None
        assert remaining.full_name == "Keep Me"
        assert ManifestEntry.query.filter_by(trip_id=trip_id).count() == 1


def test_delete_does_not_affect_the_trip(app, make_boat, make_trip):
    from app.models.trip import Trip

    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")

    with app.app_context():
        trip = db.session.get(Trip, trip_id)
        assert trip is not None
        assert trip.status == "Open"


def test_manifest_count_updates_after_delete(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, full_name="A")
    entry_to_delete = _make_manifest_entry(app, trip_id, full_name="B")
    _make_manifest_entry(app, trip_id, full_name="C")
    admin = _client_with_role(app, "Admin")

    before = admin.get(f"/manifests/{trip_id}")
    assert b"3/5 Capacity" in before.data

    admin.post(f"/manifests/{trip_id}/manifest/{entry_to_delete}/delete")

    after = admin.get(f"/manifests/{trip_id}")
    assert b"2/5 Capacity" in after.data


def test_delete_activity_log_recorded(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    admin.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")

    with app.app_context():
        logs = ActivityLog.query.filter_by(action="Deleted Passenger Manifest").all()
        assert len(logs) == 1
        assert "Juan Dela Cruz" in logs[0].details
        assert logs[0].admin_name == "Test Admin"
        assert logs[0].created_at is not None


def test_delete_cannot_target_entry_from_different_trip(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_a = make_trip(boat_id=boat_id, status="Open")
    trip_b = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_b)
    admin = _client_with_role(app, "Admin")

    response = admin.post(f"/manifests/{trip_a}/manifest/{entry_id}/delete")
    assert response.status_code == 404

    with app.app_context():
        assert db.session.get(ManifestEntry, entry_id) is not None


def test_unauthenticated_cannot_delete_manifest_entry(app, client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)

    response = client.post(
        f"/manifests/{trip_id}/manifest/{entry_id}/delete", follow_redirects=False
    )

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(ManifestEntry, entry_id) is not None


def test_delete_manifest_entry_requires_post(app, make_boat, make_trip):
    """The delete action only exists as a POST route reachable from the
    confirmation modal's form submit — there is no GET/link path that
    can delete a passenger without going through that confirmation."""
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    entry_id = _make_manifest_entry(app, trip_id)
    admin = _client_with_role(app, "Admin")

    response = admin.get(f"/manifests/{trip_id}/manifest/{entry_id}/delete")
    assert response.status_code == 405

    with app.app_context():
        assert db.session.get(ManifestEntry, entry_id) is not None


# ---------------------------------------------------------------------------
# Regression: existing functionality untouched by adding edit/delete
# ---------------------------------------------------------------------------

def test_existing_manifest_search_still_works(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    _make_manifest_entry(app, trip_id, full_name="Juan Dela Cruz", passenger_type="Regular")
    _make_manifest_entry(app, trip_id, full_name="Maria Santos", passenger_type="Senior")
    admin = _client_with_role(app, "Admin")

    response = admin.get(f"/manifests/{trip_id}?manifest_q=Senior")

    assert b"Maria Santos" in response.data
    assert b"Juan Dela Cruz" not in response.data


def test_existing_approval_workflow_still_works(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin = _client_with_role(app, "Admin")

    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id,
            full_name="New Passenger",
            age=25,
            passenger_type="Regular",
            status="pending",
        )
        db.session.add(reg)
        db.session.commit()
        reg_id = reg.id

    response = admin.post(
        f"/manifests/{trip_id}/pending/{reg_id}/approve", data={}, follow_redirects=True
    )

    assert response.status_code == 200
    with app.app_context():
        assert ManifestEntry.query.filter_by(
            trip_id=trip_id, full_name="New Passenger"
        ).count() == 1
        reg = db.session.get(PendingRegistration, reg_id)
        assert reg.status == "approved"


def test_existing_add_walkin_still_works(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    admin = _client_with_role(app, "Admin")

    response = admin.post(
        f"/manifests/{trip_id}/add-walkin",
        data={
            "full_name": "Walk-In Passenger",
            "age": "22",
            "address": "Somewhere",
            "contact_number": "09123456789",
            "passenger_type": "Regular",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert ManifestEntry.query.filter_by(
            trip_id=trip_id, full_name="Walk-In Passenger"
        ).count() == 1
