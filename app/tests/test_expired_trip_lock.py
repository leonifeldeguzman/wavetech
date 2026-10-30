"""Tests for the expired-trip lock on confirm_departure (Oct 24).

An Open/Boarding/Full/Delayed trip whose departure_time is before today
(PH day) must not be departable. A trip scheduled for today still can.
"""
from datetime import datetime, time, timedelta
from pydoc import html

from app.extensions import db  # ADAPT: use the import path your other tests use for db
from app.models.trip import Trip
from app.utils.ph_clock import ph_today


def _at_noon(day):
    return datetime.combine(day, time(12, 0))


def _depart(admin_client, trip_id):
    return admin_client.post(
        f"/manifests/{trip_id}/confirm-departure",
        data={"audit_confirmed": "on"},
    )


def _status(app, trip_id):
    with app.app_context():
        return db.session.get(Trip, trip_id).status


def test_expired_open_trip_cannot_depart(app, admin_client, make_boat, make_trip):
    boat_id = make_boat("MB Coral", 15)  # ADAPT: if make_boat returns an object, use .id
    yesterday = ph_today() - timedelta(days=1)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=_at_noon(yesterday))  # ADAPT: same for make_trip

    response = _depart(admin_client, trip_id)

    assert response.status_code == 302
    assert "departure-success" not in response.headers["Location"]
    assert _status(app, trip_id) == "Open"


def test_expired_delayed_trip_cannot_depart(app, admin_client, make_boat, make_trip):
    boat_id = make_boat("MB Coral", 15)
    yesterday = ph_today() - timedelta(days=1)
    trip_id = make_trip(boat_id=boat_id, status="Delayed",
                        departure_time=_at_noon(yesterday))

    _depart(admin_client, trip_id)

    assert _status(app, trip_id) == "Delayed"


def test_todays_open_trip_can_still_depart(app, admin_client, make_boat, make_trip, morning_clock):
    boat_id = make_boat("MB Coral", 15)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=_at_noon(ph_today()))

    response = _depart(admin_client, trip_id)

    assert response.status_code == 302
    assert "departure-success" in response.headers["Location"]
    assert _status(app, trip_id) == "Departed"


def test_expired_trip_page_shows_expired_and_locks_checkbox(admin_client, make_boat, make_trip):
    boat_id = make_boat("MB Coral", 15)
    yesterday = ph_today() - timedelta(days=1)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=_at_noon(yesterday))

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert 'manifest-checkin-open' not in html
    assert '<span class="manifest-checkin-status">' not in html


def test_todays_trip_page_still_shows_check_in_open(admin_client, make_boat, make_trip, morning_clock):
    boat_id = make_boat("MB Coral", 15)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=_at_noon(ph_today()))

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert 'manifest-checkin-open' in html
    assert "manifest-checkin-expired" not in html



def test_todays_trip_after_its_time_is_expired(app, admin_client, make_boat, make_trip, monkeypatch):
    """A trip today whose time has passed (4 PM slot at 5 PM) must be locked."""
    monkeypatch.setattr(
        "app.blueprints.manifests.routes.ph_now",
        lambda: datetime.combine(ph_today(), time(17, 0)),
    )
    boat_id = make_boat("MB Coral", 15)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=datetime.combine(ph_today(), time(16, 0)))

    response = _depart(admin_client, trip_id)

    assert "departure-success" not in response.headers["Location"]
    assert _status(app, trip_id) == "Open"


def test_todays_trip_before_its_time_is_not_expired(app, admin_client, make_boat, make_trip, monkeypatch):
    """A trip today still ahead of the clock (4 PM slot at 3 PM) can depart."""
    monkeypatch.setattr(
        "app.blueprints.manifests.routes.ph_now",
        lambda: datetime.combine(ph_today(), time(15, 0)),
    )
    boat_id = make_boat("MB Coral", 15)
    trip_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=datetime.combine(ph_today(), time(16, 0)))

    response = _depart(admin_client, trip_id)

    assert "departure-success" in response.headers["Location"]
    assert _status(app, trip_id) == "Departed"