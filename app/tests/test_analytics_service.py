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

@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(svc, "ph_now", lambda: datetime(2026, 5, 29, 8, 0))


def test_summary_counts_passengers_and_trips():
    b = _boat(capacity=10)
    _pax(_trip(b, 10, "Departed"), 5)
    _pax(_trip(b, 12, "Open"), 3)
    s = svc.get_daily_summary(DAY)
    assert s["daily_passengers"] == 8
    assert s["trips_today"] == 2
    assert s["overall_capacity_pct"] == 40.0  # 8 / 20 seats


def test_avg_capacity_includes_empty_open_trips():
    b = _boat(capacity=10)
    _pax(_trip(b, 10, "Departed"), 8)   # 80%
    _pax(_trip(b, 12, "Boarding"), 6)   # 60%
    _trip(b, 16, "Open")                # empty, now counted as 0%
    assert svc.get_daily_summary(DAY)["avg_capacity_pct"] == 46.7


def test_cancelled_trips_counted_in_trips_but_not_passengers():
    b = _boat()
    t = _trip(b, 10, "Cancelled", cancel_reason="Weather")
    _pax(t, 4)
    s = svc.get_daily_summary(DAY)
    assert s["trips_today"] == 1
    assert s["cancelled_trips"] == 1
    assert s["daily_passengers"] == 0
    


def test_status_summary_uses_current_status_only():
    b = _boat()
    _trip(b, 10, "Departed", delay_reason="Strong winds")  # delayed earlier, now departed
    _trip(b, 12, "Delayed")
    _trip(b, 16, "Open")
    s = svc.get_daily_summary(DAY)
    assert s["departed_trips"] == 1
    assert s["delayed_trips"] == 1

def test_unassigned_trip_not_counted():
    b = _boat()
    _pax(_trip(b, 10, "Departed"), 4)
    _trip(None, 12, "Open")             # no boat = unassigned
    s = svc.get_daily_summary(DAY)
    assert s["trips_today"] == 1
    assert s["trips_caption"] == "Departed 1"


def test_expired_trip_not_counted(monkeypatch):
    b = _boat()
    _trip(b, 10, "Open")
    assert svc.get_daily_summary(DAY)["trips_today"] == 1   # 8:00 AM, not expired
    monkeypatch.setattr(svc, "ph_now", lambda: datetime(2026, 5, 29, 11, 0))
    assert svc.get_daily_summary(DAY)["trips_today"] == 0   # 11:00 AM, expired


def test_trip_caption_lists_only_nonzero_statuses():
    b = _boat()
    _pax(_trip(b, 10, "Departed"), 2)
    _trip(b, 12, "Open")
    assert svc.get_daily_summary(DAY)["trips_caption"] == "Open 1, Departed 1"


def test_yesterday_passengers_value_or_none():
    b = _boat()
    _pax(_trip(b, 10, "Departed", day=DAY - timedelta(days=1)), 5)
    assert svc.get_daily_summary(DAY)["yesterday_passengers"] == 5
    assert svc.get_daily_summary(DAY + timedelta(days=5))["yesterday_passengers"] is None


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


# ---------------------------------------------------------------------------
# Passenger Flow chart (Batch 4)
# ---------------------------------------------------------------------------

def test_flow_chart_includes_only_counted_assigned_trips():
    b = _boat("MB Coral")
    _pax(_trip(b, 10, "Departed"), 5)
    _pax(_trip(b, 12, "Open"), 2)
    _trip(b, 14, "Cancelled", cancel_reason="Weather")
    _trip(None, 15, "Open")                      # unassigned
    _trip(b, 7, "Open")                          # expired (clock is 8:00 AM)
    rows = svc.get_flow_chart(DAY)
    assert [(r["label"], r["passengers"]) for r in rows] == [
        ("10:00 AM, MB Coral", 5),
        ("12:00 PM, MB Coral", 2),
    ]


def test_flow_chart_plots_delayed_trip_at_new_time():
    b = _boat("MB Horizon")
    _trip(b, 9, "Delayed", new_departure_time=datetime(2026, 5, 29, 13, 0))
    _trip(b, 11, "Open")
    rows = svc.get_flow_chart(DAY)
    assert [r["label"] for r in rows] == [
        "11:00 AM, MB Horizon",
        "1:00 PM, MB Horizon",
    ]


def test_delayed_trip_is_not_expired_until_its_new_time(monkeypatch):
    b = _boat()
    _trip(b, 9, "Delayed", new_departure_time=datetime(2026, 5, 29, 13, 0))
    monkeypatch.setattr(svc, "ph_now", lambda: datetime(2026, 5, 29, 11, 0))
    assert svc.get_daily_summary(DAY)["trips_today"] == 1   # new time is 1 PM
    monkeypatch.setattr(svc, "ph_now", lambda: datetime(2026, 5, 29, 14, 0))
    assert svc.get_daily_summary(DAY)["trips_today"] == 0   # now expired


def test_flow_chart_empty_when_nothing_qualifies():
    _trip(None, 10, "Open")
    assert svc.get_flow_chart(DAY) == []


# ---------------------------------------------------------------------------
# Capacity Utilization chart (Batch 4)
# ---------------------------------------------------------------------------

def test_capacity_chart_label_and_percentage():
    b = _boat("MB Coral", capacity=15)
    _pax(_trip(b, 10, "Departed"), 2)
    row = svc.get_capacity_chart(DAY)[0]
    assert row["label"] == "10:00 AM, MB Coral"
    assert row["count_label"] == "2 / 15, 13%"
    assert row["capacity_pct"] == 13.3
    assert row["is_full"] is False


def test_capacity_chart_marks_full_trips():
    b = _boat("MB Blue", capacity=10)
    _pax(_trip(b, 12, "Full"), 10)
    row = svc.get_capacity_chart(DAY)[0]
    assert row["is_full"] is True
    assert row["count_label"] == "10 / 10, 100%"


def test_capacity_chart_excludes_cancelled_unassigned_and_expired():
    b = _boat("MB Coral")
    _pax(_trip(b, 10, "Departed"), 5)
    _trip(b, 14, "Cancelled", cancel_reason="Weather")
    _trip(None, 15, "Open")                      # unassigned
    _trip(b, 7, "Open")                          # expired (clock is 8:00 AM)
    rows = svc.get_capacity_chart(DAY)
    assert [r["label"] for r in rows] == ["10:00 AM, MB Coral"]


def test_capacity_chart_sorted_and_delayed_uses_new_time():
    b = _boat("MB Horizon")
    _trip(b, 9, "Delayed", new_departure_time=datetime(2026, 5, 29, 13, 0))
    _trip(b, 11, "Open")
    rows = svc.get_capacity_chart(DAY)
    assert [r["label"] for r in rows] == [
        "11:00 AM, MB Horizon",
        "1:00 PM, MB Horizon",
    ]


def test_capacity_chart_empty_when_nothing_qualifies():
    _trip(None, 10, "Open")
    assert svc.get_capacity_chart(DAY) == []

# ---------------------------------------------------------------------------
# Passenger Trend chart (Batch 4)
# ---------------------------------------------------------------------------

def test_trend_chart_has_seven_days_oldest_first():
    rows = svc.get_trend_chart(DAY)
    assert len(rows) == 7
    assert rows[0]["date"] == DAY - timedelta(days=6)
    assert rows[-1]["date"] == DAY
    assert rows[-1]["label"] == "May 29"


def test_trend_chart_blank_not_zero_for_days_without_data():
    b = _boat()
    _pax(_trip(b, 10, "Departed", day=DAY - timedelta(days=2)), 4)
    rows = svc.get_trend_chart(DAY)
    by_date = {r["date"]: r["passengers"] for r in rows}
    assert by_date[DAY - timedelta(days=2)] == 4
    assert by_date[DAY - timedelta(days=1)] is None
    assert by_date[DAY] is None


def test_trend_chart_real_zero_when_trip_has_no_passengers():
    b = _boat()
    _trip(b, 10, "Departed", day=DAY - timedelta(days=1))
    by_date = {r["date"]: r["passengers"] for r in svc.get_trend_chart(DAY)}
    assert by_date[DAY - timedelta(days=1)] == 0


def test_trend_chart_ignores_cancelled_and_unassigned():
    b = _boat()
    _pax(_trip(b, 10, "Cancelled", cancel_reason="Weather"), 5)
    _pax(_trip(None, 12, "Open"), 3)
    by_date = {r["date"]: r["passengers"] for r in svc.get_trend_chart(DAY)}
    assert by_date[DAY] is None

def test_records_hide_unassigned_trips():
    b = _boat()
    kept = _trip(b, 9, "Departed")
    _trip(None, 10, "Open")
    _trip(None, 11, "Cancelled", cancel_reason="Weather")
    ids = [r["trip_id"] for r in svc.get_manifest_records(today=DAY)["rows"]]
    assert ids == [kept.id]


def test_records_hide_expired_but_keep_departed_and_cancelled():
    b = _boat()
    _trip(b, 6, "Open")                                    # expired (clock is 8:00 AM)
    _trip(b, 7, "Full")                                    # expired
    _trip(b, 5, "Departed")                                # kept
    _trip(b, 4, "Cancelled", cancel_reason="Weather")      # kept
    _trip(b, 15, "Open")                                   # upcoming, kept
    rows = svc.get_manifest_records(today=DAY)["rows"]
    assert [(r["time_label"], r["status"]) for r in rows] == [
        ("4:00 AM", "Cancelled"),
        ("5:00 AM", "Departed"),
        ("3:00 PM", "Open"),
    ]


def test_records_hide_open_trip_from_a_past_day():
    b = _boat()
    _trip(b, 9, "Open", day=DAY - timedelta(days=1))       # never departed
    kept = _trip(b, 9, "Departed", day=DAY - timedelta(days=1), ph_minute=30)
    ids = [r["trip_id"] for r in svc.get_manifest_records(today=DAY)["rows"]]
    assert ids == [kept.id]


def test_records_delayed_trip_expires_at_its_new_time(monkeypatch):
    b = _boat()
    _trip(b, 6, "Delayed", new_departure_time=datetime(2026, 5, 29, 13, 0))
    assert len(svc.get_manifest_records(today=DAY)["rows"]) == 1   # 8:00 AM, new time is 1 PM
    monkeypatch.setattr(svc, "ph_now", lambda: datetime(2026, 5, 29, 14, 0))
    assert svc.get_manifest_records(today=DAY)["rows"] == []       # 2:00 PM, expired