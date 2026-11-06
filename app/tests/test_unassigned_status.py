"""Unassigned vs Open status for timetable slots.

A stored-"Open" trip is only shown (and treated) as Open once boat, operator
AND crew are all assigned; otherwise it is Unassigned. Other statuses are
never changed.

Run with:
    python -m pytest app/tests/test_unassigned_status.py -v
"""
import json
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip
from app.services import manifest_service
from app.utils.ph_clock import ph_today


def _future_slot():
    # Tomorrow, so the slot is never "Expired" and is still registrable.
    return datetime.combine(ph_today() + timedelta(days=1), time(10, 0))


def _status_of(app, trip_id):
    with app.app_context():
        return manifest_service.effective_status(db.session.get(Trip, trip_id))


def _list_html(client):
    day = (ph_today() + timedelta(days=1)).isoformat()
    return client.get(f"/manifests?date={day}").data


def _slot(make_trip, make_boat, boat=True, operator=None, crew=None, status="Open"):
    return make_trip(
        boat_id=make_boat(capacity=5) if boat else None,
        departure_time=_future_slot(),
        status=status,
        operator_name=operator,
        crew_name=crew,
    )


# --- 1-5: Unassigned vs Open ------------------------------------------------

def test_new_slot_without_assignments_is_unassigned(app, make_trip, make_boat):
    assert _status_of(app, _slot(make_trip, make_boat, boat=False)) == "Unassigned"


def test_boat_only_is_unassigned(app, make_trip, make_boat):
    assert _status_of(app, _slot(make_trip, make_boat)) == "Unassigned"


def test_boat_and_operator_without_crew_is_unassigned(app, make_trip, make_boat):
    assert _status_of(app, _slot(make_trip, make_boat, operator="Capt. A")) == "Unassigned"


def test_boat_and_crew_without_operator_is_unassigned(app, make_trip, make_boat):
    assert _status_of(app, _slot(make_trip, make_boat, crew="Crew A")) == "Unassigned"


def test_operator_and_crew_without_boat_is_unassigned(app, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, boat=False, operator="Capt. A", crew="Crew A")
    assert _status_of(app, slot) == "Unassigned"


def test_complete_assignment_is_open(app, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, operator="Capt. A", crew="Crew A")
    assert _status_of(app, slot) == "Open"


def test_blank_or_spaces_only_values_count_as_missing(app, make_trip, make_boat):
    assert _status_of(app, _slot(make_trip, make_boat, operator="   ", crew="Crew A")) == "Unassigned"
    assert _status_of(app, _slot(make_trip, make_boat, operator="Capt. A", crew="")) == "Unassigned"


# --- 6: other statuses are preserved ----------------------------------------

@pytest.mark.parametrize("status", ["Boarding", "Full", "Delayed", "Cancelled", "Departed"])
def test_other_statuses_stay_unchanged_even_if_unassigned(app, make_trip, make_boat, status):
    slot = _slot(make_trip, make_boat, boat=False, status=status)
    assert _status_of(app, slot) == status
    with app.app_context():
        assert db.session.get(Trip, slot).status == status  # stored value untouched


# --- list page ----------------------------------------------------------------

def test_list_shows_unassigned_and_open(app, admin_client, make_trip, make_boat):
    unassigned = _slot(make_trip, make_boat)
    html = _list_html(admin_client)
    assert b"status-unassigned" in html and b"Unassigned" in html
    assert b"status-open" not in html

    with app.app_context():
        trip = db.session.get(Trip, unassigned)
        trip.operator_name, trip.crew_name = "Capt. A", "Crew A"
        db.session.commit()
    html = _list_html(admin_client)
    assert b"status-open" in html
    assert b"status-unassigned" not in html


def test_stored_status_is_not_rewritten_by_viewing_list(app, admin_client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, boat=False)
    _list_html(admin_client)
    with app.app_context():
        assert db.session.get(Trip, slot).status == "Open"


# --- 8: completing the assignment through the real form ----------------------

def test_saving_complete_trip_details_makes_slot_open(app, admin_client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, boat=False)
    assert _status_of(app, slot) == "Unassigned"

    resp = admin_client.post(f"/manifests/{slot}/assign-boat", data={
        "boat_name": "Sea Star", "boat_capacity": "10",
        "operator_name": "Capt. A", "crew_name": "Crew A",
    })

    assert resp.status_code == 302
    assert _status_of(app, slot) == "Open"


def test_saving_without_crew_keeps_slot_unassigned(app, admin_client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, boat=False)

    admin_client.post(f"/manifests/{slot}/assign-boat", data={
        "boat_name": "Sea Star", "boat_capacity": "10",
        "operator_name": "Capt. A", "crew_name": "",
    })

    assert _status_of(app, slot) == "Unassigned"


# --- 7: passenger registration ------------------------------------------------

def _register(client, trip_id):
    return client.post(
        "/api/passenger/registrations",
        data=json.dumps({
            "trip_id": trip_id, "full_name": "Test Passenger", "age": 21,
            "address": "Test Address", "contact_number": "09123456789",
            "passenger_type": "Regular",
        }),
        content_type="application/json",
    )


def test_unassigned_slot_cannot_be_registered(app, client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, operator="Capt. A")  # boat + operator, no crew

    body = _register(client, slot).get_json()

    assert body["success"] is False
    with app.app_context():
        assert PendingRegistration.query.filter_by(trip_id=slot).count() == 0


def test_unassigned_slot_is_not_offered_for_registration(client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, operator="Capt. A")

    data = client.get(f"/api/trips/{slot}").get_json()["data"]

    assert data["accepts_registration"] is False


def test_complete_slot_can_be_registered(app, client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat, operator="Capt. A", crew="Crew A")

    body = _register(client, slot).get_json()

    assert body["success"] is True
    with app.app_context():
        assert PendingRegistration.query.filter_by(trip_id=slot).count() == 1


# --- trip detail page ---------------------------------------------------------

def test_detail_page_shows_unassigned_then_open(app, admin_client, make_trip, make_boat):
    slot = _slot(make_trip, make_boat)  # boat only

    html = admin_client.get(f"/manifests/{slot}").data
    assert b"manifest-checkin-unassigned" in html
    assert b"manifest-checkin-open" not in html

    with app.app_context():
        trip = db.session.get(Trip, slot)
        trip.operator_name, trip.crew_name = "Capt. A", "Crew A"
        db.session.commit()

    html = admin_client.get(f"/manifests/{slot}").data
    assert b"manifest-checkin-open" in html
    assert b"manifest-checkin-unassigned" not in html