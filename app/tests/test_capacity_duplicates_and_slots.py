"""Add Slot past-date rule, duplicate passengers, capacity-full banner and
the derived Full status.

Run with:
    python -m pytest app/tests/test_capacity_duplicates_and_slots.py -v
"""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip
from app.services import manifest_service
from app.utils.ph_clock import ph_today


def _tomorrow_slot():
    return datetime.combine(ph_today() + timedelta(days=1), time(10, 0))


def _trip(make_trip, make_boat, capacity, **kwargs):
    return make_trip(
        boat_id=make_boat(capacity=capacity),
        departure_time=_tomorrow_slot(),
        **kwargs,
    )


def _entry(app, trip_id, name="Juan Dela Cruz", age=30, contact="09123456789"):
    with app.app_context():
        db.session.add(ManifestEntry(
            trip_id=trip_id, full_name=name, age=age,
            contact_number=contact, source="walkin",
        ))
        db.session.commit()


def _count(app, trip_id):
    with app.app_context():
        return ManifestEntry.query.filter_by(trip_id=trip_id).count()


def _walkin(client, trip_id, name="Maria Santos", age="25", contact="09170000000"):
    return client.post(f"/manifests/{trip_id}/add-walkin", data={
        "full_name": name, "age": age, "address": "x",
        "contact_number": contact, "passenger_type": "Regular",
    })


def _status(app, trip_id, count=None):
    with app.app_context():
        return manifest_service.effective_status(db.session.get(Trip, trip_id), count)


# --- 2. Add Time Slot and past dates -------------------------------------------

def test_timetable_disables_add_for_past_date(admin_client):
    yesterday = (ph_today() - timedelta(days=1)).isoformat()
    html = admin_client.get(f"/manifests?date={yesterday}").get_data(as_text=True)
    button = html.split('id="addSlotBtn"', 1)[1].split(">", 1)[0]
    assert "disabled" in button
    assert "data-bs-toggle" not in button          # cannot open the modal


@pytest.mark.parametrize("offset_days", [0, 1, 30])
def test_timetable_enables_add_for_today_and_future(admin_client, offset_days):
    day = (ph_today() + timedelta(days=offset_days)).isoformat()
    html = admin_client.get(f"/manifests?date={day}").get_data(as_text=True)
    button = html.split('id="addSlotBtn"', 1)[1].split(">", 1)[0]
    assert "disabled" not in button
    assert 'data-bs-target="#addSlotModal"' in button


def test_backend_refuses_slot_on_past_date(app, admin_client):
    yesterday = (ph_today() - timedelta(days=1)).isoformat()
    resp = admin_client.post("/manifests/add-slot", data={
        "slot_date": yesterday, "departure_time": "10:00", "view_date": yesterday,
    })
    assert resp.status_code == 400
    with app.app_context():
        assert Trip.query.count() == 0


def test_backend_still_creates_future_slot(app, admin_client):
    tomorrow = (ph_today() + timedelta(days=1)).isoformat()
    resp = admin_client.post("/manifests/add-slot", data={
        "slot_date": tomorrow, "departure_time": "10:00", "view_date": tomorrow,
    })
    assert resp.status_code == 302
    with app.app_context():
        assert Trip.query.count() == 1


# --- 3. Duplicate passengers ---------------------------------------------------

@pytest.mark.parametrize("variant", [
    "juan dela cruz", "JUAN DELA CRUZ", "  Juan Dela Cruz  ", "Juan  Dela Cruz",
])
def test_walkin_duplicate_name_is_refused(app, admin_client, make_trip, make_boat, variant):
    trip_id = _trip(make_trip, make_boat, 10)
    _entry(app, trip_id)

    resp = _walkin(admin_client, trip_id, name=variant, age="45", contact="09999999999")

    assert resp.status_code == 400
    assert manifest_service.MSG_DUPLICATE_NAME in resp.get_data(as_text=True)
    assert _count(app, trip_id) == 1


def test_walkin_same_person_gets_registered_message(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 10)
    _entry(app, trip_id, age=30, contact="0912-345-6789")

    resp = _walkin(admin_client, trip_id, name="juan dela cruz", age="30", contact="09123456789")

    assert resp.status_code == 400
    assert manifest_service.MSG_DUPLICATE_PASSENGER in resp.get_data(as_text=True)
    assert _count(app, trip_id) == 1


def test_same_name_on_a_different_trip_is_allowed(app, admin_client, make_trip, make_boat):
    first = _trip(make_trip, make_boat, 10)
    second = make_trip(
        boat_id=make_boat(name="Other", capacity=10),
        departure_time=_tomorrow_slot() + timedelta(hours=1),
    )
    _entry(app, first)

    resp = _walkin(admin_client, second, name="Juan Dela Cruz")

    assert resp.status_code == 302
    assert _count(app, second) == 1


def test_different_names_are_not_merged(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 10)
    _entry(app, trip_id)

    assert _walkin(admin_client, trip_id, name="Juan Dela Cruz Jr.").status_code == 302
    assert _count(app, trip_id) == 2


def test_approving_a_duplicate_registration_is_refused(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 10)
    _entry(app, trip_id)
    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id, full_name="JUAN  dela cruz", age=30,
            contact_number="09123456789", passenger_type="Regular",
            status="pending",
        )
        db.session.add(reg)
        db.session.commit()
        reg_id = reg.id

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve")

    assert _count(app, trip_id) == 1
    with app.app_context():
        assert db.session.get(PendingRegistration, reg_id).status == "pending"


def test_editing_into_a_duplicate_name_is_refused(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 10)
    _entry(app, trip_id, name="Juan Dela Cruz")
    _entry(app, trip_id, name="Maria Santos", contact="09111111111")
    with app.app_context():
        other = ManifestEntry.query.filter_by(full_name="Maria Santos").one().id

    admin_client.post(f"/manifests/{trip_id}/manifest/{other}/edit", data={
        "full_name": "juan dela cruz", "age": "30", "address": "x",
        "contact_number": "09111111111", "passenger_type": "Regular",
    })

    with app.app_context():
        assert db.session.get(ManifestEntry, other).full_name == "Maria Santos"


# --- 4. Capacity banner and the Add button -------------------------------------

def _detail(client, trip_id):
    return client.get(f"/manifests/{trip_id}").get_data(as_text=True)


@pytest.mark.parametrize("count,capacity,full", [
    (8, 12, False), (11, 12, False), (12, 12, True), (13, 12, True), (12, 15, False),
])
def test_banner_and_add_button_follow_capacity(
    app, admin_client, make_trip, make_boat, count, capacity, full
):
    trip_id = _trip(make_trip, make_boat, capacity)
    for i in range(count):
        _entry(app, trip_id, name=f"Passenger {i}", contact=f"0917000{i:04d}")

    html = _detail(admin_client, trip_id)

    assert ("capacityFullBanner" in html) is full
    # The button, not its label: "Edit Capacity" also appears in a JS comment on every page.
    assert ('id="editCapacityBtn"' in html) is full
    button = html.split('id="addPassengerBtn"', 1)[1].split(">", 1)[0]
    assert ("disabled" in button) is full
    assert ('data-bs-target="#walkinModal"' in button) is (not full)


def test_pending_registrations_hold_a_seat(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 2)
    _entry(app, trip_id)
    with app.app_context():
        db.session.add_all([
            PendingRegistration(trip_id=trip_id, full_name="Pending One", age=20,
                                passenger_type="Regular", status="pending"),
            PendingRegistration(trip_id=trip_id, full_name="Rejected One", age=20,
                                passenger_type="Regular", status="rejected"),
            PendingRegistration(trip_id=trip_id, full_name="Cancelled One", age=20,
                                passenger_type="Regular", status="cancelled"),
        ])
        db.session.commit()
        assert manifest_service.occupied_seats(trip_id) == 2   # rejected/cancelled ignored


def test_backend_refuses_walkin_when_full(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 1)
    _entry(app, trip_id)

    _walkin(admin_client, trip_id, name="Someone Else")

    assert _count(app, trip_id) == 1


# --- 5. Full status ------------------------------------------------------------

def test_timetable_shows_full_and_updates_with_capacity(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 2)
    _entry(app, trip_id, name="A One", contact="09170000001")
    _entry(app, trip_id, name="B Two", contact="09170000002")
    day = _tomorrow_slot().date().isoformat()

    assert "status-full" in admin_client.get(f"/manifests?date={day}").get_data(as_text=True)
    assert _status(app, trip_id) == "Full"

    admin_client.post(f"/manifests/{trip_id}/assign-boat", data={
        "boat_name": "Test Boat", "boat_capacity": "5",
        "operator_name": "Test Operator", "crew_name": "Test Crew",
    })
    assert "status-full" not in admin_client.get(f"/manifests?date={day}").get_data(as_text=True)
    assert _status(app, trip_id) == "Open"


def test_deleting_a_passenger_frees_a_full_trip(app, admin_client, make_trip, make_boat):
    trip_id = _trip(make_trip, make_boat, 1)
    _entry(app, trip_id)
    with app.app_context():
        entry_id = ManifestEntry.query.filter_by(trip_id=trip_id).one().id
        trip = db.session.get(Trip, trip_id)
        manifest_service.sync_trip_status(trip)
        db.session.commit()
        assert db.session.get(Trip, trip_id).status == "Full"

    admin_client.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")

    with app.app_context():
        assert db.session.get(Trip, trip_id).status == "Open"


@pytest.mark.parametrize("status", ["Cancelled", "Departed", "Delayed", "Boarding"])
def test_full_never_overwrites_lifecycle_statuses(app, make_trip, make_boat, status):
    trip_id = _trip(make_trip, make_boat, 1, status=status)
    _entry(app, trip_id)
    assert _status(app, trip_id) == status