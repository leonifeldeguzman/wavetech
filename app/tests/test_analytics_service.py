from datetime import date, datetime, timedelta

import pytest

from app.extensions import db
from app.models.boat import Boat
from app.models.manifest_entry import ManifestEntry
from app.models.trip import Trip
from app.services import analytics_service as svc

DAY = date(2026, 5, 29)


def _ph_time(day, hour, minute=0):
    """Trip.departure_time is stored as naive PH wall-clock time."""
    return datetime(day.year, day.month, day.day, hour, minute)


def _boat(name="MB Coral", capacity=10):
    b = Boat(name=name, capacity=capacity)
    db.session.add(b)
    db.session.commit()
    return b


def _trip(boat, ph_hour, status="Open", day=DAY, ph_minute=0, **kw):
    t = Trip(boat_id=boat.id if boat else None,
             departure_time=_ph_time(day, ph_hour, ph_minute),
             status=status, **kw)
    db.session.add(t)
    db.session.commit()
    return t


def _pax(trip, n):
    for i in range(n):
        db.session.add(ManifestEntry(trip_id=trip.id, full_name=f"P{i}", age=20))
    db.session.commit()


# ADAPT: relies on the `app` fixture in your conftest.py (disposable SQLite)
@pytest.fixture(autouse=True)
def _ctx(app):
    with app.app_context():
        yield


def test_summary_counts_passengers_and_trips():
    b = _boat(capacity=10)
    _pax(_trip(b, 10, "Departed"), 5)
    _pax(_trip(b, 12, "Open"), 3)
    s = svc.get_daily_summary(DAY)
    assert s["daily_passengers"] == 8
    assert s["trips_today"] == 2
    assert s["overall_capacity_pct"] == 40.0  # 8 / 20 seats


def test_avg_capacity_ignores_empty_future_trips():
    b = _boat(capacity=10)
    _pax(_trip(b, 10, "Departed"), 8)   # 80%
    _pax(_trip(b, 12, "Boarding"), 6)   # 60%
    _trip(b, 16, "Open")                # empty, not counted
    assert svc.get_daily_summary(DAY)["avg_capacity_pct"] == 70.0


def test_cancelled_trips_excluded():
    b = _boat()
    t = _trip(b, 10, "Cancelled", cancel_reason="Weather")
    _pax(t, 4)
    s = svc.get_daily_summary(DAY)
    assert s["trips_today"] == 0
    assert s["daily_passengers"] == 0
    assert svc.get_trip_breakdown(DAY) == []


def test_delayed_counts_trip_that_later_departed():
    b = _boat()
    _trip(b, 10, "Departed", delay_reason="Strong winds")
    _trip(b, 12, "Delayed")
    _trip(b, 16, "Open")
    assert svc.get_daily_summary(DAY)["delayed_trips"] == 2


def test_vs_yesterday_and_dash_when_no_data():
    b = _boat(capacity=20)
    _pax(_trip(b, 10, "Departed", day=DAY - timedelta(days=1)), 10)
    _pax(_trip(b, 10, "Departed"), 15)
    assert svc.get_daily_summary(DAY)["passengers_change_pct"] == 50
    # no yesterday data -> None, not 0 or infinity
    assert svc.get_daily_summary(DAY + timedelta(days=5))["passengers_change_pct"] is None


def test_ph_day_boundaries():
    b = _boat()
    _trip(b, 0, "Departed", ph_minute=30)                      # 12:30 AM PH -> in DAY
    _trip(b, 23, "Departed", ph_minute=30)                     # 11:30 PM PH -> in DAY
    _trip(b, 0, "Departed", day=DAY + timedelta(days=1))       # next PH day
    assert svc.get_daily_summary(DAY)["trips_today"] == 2


def test_trip_without_boat_has_no_percentage_and_no_crash():
    t = _trip(None, 10, "Departed")
    _pax(t, 3)
    rows = svc.get_trip_breakdown(DAY)
    assert rows[0]["capacity_pct"] is None
    assert svc.get_daily_summary(DAY)["avg_capacity_pct"] is None


def test_breakdown_ordered_by_departure_and_shows_ph_time():
    b = _boat(capacity=10)
    _pax(_trip(b, 12, "Open"), 2)
    _pax(_trip(b, 10, "Departed"), 5)
    rows = svc.get_trip_breakdown(DAY)
    assert [r["departure_ph"].hour for r in rows] == [10, 12]
    assert rows[0]["capacity_pct"] == 50.0


# ---------------------------------------------------------------------------
# Manifest Records (Step 4)
# ---------------------------------------------------------------------------

def _named(trip, name, age=30, ptype="Regular"):
    db.session.add(ManifestEntry(trip_id=trip.id, full_name=name, age=age,
                                 passenger_type=ptype))
    db.session.commit()


def test_records_newest_date_first_and_show_date_only_on_first_row():
    b = _boat(capacity=10)
    _trip(b, 12, "Departed")
    _trip(b, 9, "Departed", ph_minute=30)
    _trip(b, 9, "Departed", day=DAY - timedelta(days=1), ph_minute=30)
    rows = svc.get_manifest_records(today=DAY)["rows"]
    assert [(r["date_label"], r["time_label"], r["show_date"]) for r in rows] == [
        ("May 29, 2026", "9:30 AM", True),
        ("May 29, 2026", "12:00 PM", False),
        ("May 28, 2026", "9:30 AM", True),
    ]


def test_records_include_cancelled_trips_and_show_counts():
    b = _boat(capacity=10)
    _pax(_trip(b, 10, "Cancelled", cancel_reason="Weather"), 4)
    row = svc.get_manifest_records(today=DAY)["rows"][0]
    assert row["status"] == "Cancelled"
    assert (row["passengers"], row["capacity"]) == (4, 10)


def test_records_search_by_name_age_and_type_returns_matching_trips_only():
    b = _boat()
    t1 = _trip(b, 9)
    t2 = _trip(b, 12)
    _named(t1, "Maria Santos", age=41, ptype="Regular")
    _named(t2, "Felicia Ong", age=62, ptype="Senior")

    def ids(term):
        return [r["trip_id"] for r in svc.get_manifest_records(search=term, today=DAY)["rows"]]

    assert ids("maria") == [t1.id]          # case-insensitive, partial
    assert ids("62") == [t2.id]             # exact age
    assert ids("senior") == [t2.id]         # passenger type
    assert ids("nobody") == []
    assert sorted(ids("")) == sorted([t1.id, t2.id])


def test_records_filter_by_boat_status_and_date_range():
    coral, blue = _boat("MB Coral"), _boat("MB Blue")
    a = _trip(coral, 9, "Departed")
    _trip(blue, 10, "Cancelled")
    _trip(coral, 9, "Departed", day=DAY - timedelta(days=5))

    def ids(**kw):
        return [r["trip_id"] for r in svc.get_manifest_records(today=DAY, **kw)["rows"]]

    assert len(ids(boat_id=coral.id)) == 2
    assert len(ids(status="Cancelled")) == 1
    assert ids(date_from=DAY, date_to=DAY, boat_id=coral.id) == [a.id]
    # reversed dates are swapped instead of returning nothing:
    # all 3 trips fall between DAY-5 and DAY
    assert len(ids(date_from=DAY, date_to=DAY - timedelta(days=5))) == 3


def test_records_default_window_is_last_30_days_and_no_future_trips():
    b = _boat()
    _trip(b, 9, "Departed", day=DAY - timedelta(days=29))   # inside window
    _trip(b, 9, "Departed", day=DAY - timedelta(days=30))   # too old
    _trip(b, 9, "Open", day=DAY + timedelta(days=1))        # future
    assert len(svc.get_manifest_records(today=DAY)["rows"]) == 1


def test_records_truncation_flag(monkeypatch):
    monkeypatch.setattr(svc, "MAX_RECORD_ROWS", 2)
    b = _boat()
    for h in (9, 10, 11):
        _trip(b, h, "Departed")
    res = svc.get_manifest_records(today=DAY)
    assert len(res["rows"]) == 2
    assert res["truncated"] is True


# ---------------------------------------------------------------------------
# Manifest Details (Step 5)
# ---------------------------------------------------------------------------

def test_manifest_detail_fields_and_numbering_by_check_in_order():
    b = _boat("MB Coral", capacity=15)
    t = _trip(b, 10, "Departed", crew_name="Juan Dela Cruz")
    db.session.add_all([
        ManifestEntry(trip_id=t.id, full_name="Second", age=62, address="Brgy. Ibaba",
                      passenger_type="Senior", check_in_time=datetime(2026, 5, 29, 9, 55, 38)),
        ManifestEntry(trip_id=t.id, full_name="First", age=19, address="Brgy. Tagapo",
                      passenger_type="Student", check_in_time=datetime(2026, 5, 29, 9, 47, 12)),
    ])
    db.session.commit()

    d = svc.get_manifest_detail(t.id)
    assert (d["date_label"], d["time_label"]) == ("May 29, 2026", "10:00 AM")
    assert (d["boat_name"], d["driver_name"], d["capacity"], d["passengers"], d["status"]) == (
        "MB Coral", "Juan Dela Cruz", 15, 2, "Departed")
    assert [(p["no"], p["full_name"], p["check_in_label"]) for p in d["passenger_list"]] == [
        (1, "First", "09:47:12"), (2, "Second", "09:55:38")]


def test_manifest_detail_unknown_trip_is_none():
    assert svc.get_manifest_detail(99999) is None


def test_manifest_detail_without_boat_or_passengers():
    t = _trip(None, 10, "Open")
    d = svc.get_manifest_detail(t.id)
    assert d["boat_name"] is None and d["capacity"] is None
    assert d["passengers"] == 0 and d["passenger_list"] == []