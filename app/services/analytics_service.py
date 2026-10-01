"""Analytics queries for Reports & Analytics (read-only, no UI).

All "today" logic uses the Philippine calendar day.
Trip.departure_time is stored as naive PH wall-clock time (confirmed in
flask shell: a 9:30 AM slot is stored as 09:30). The current moment, however,
comes from the server clock, which is UTC on cloud hosts such as Vercel, so
ph_today() (app/utils/ph_clock.py) converts.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.extensions import db
from app.models.manifest_entry import ManifestEntry
from app.models.trip import Trip
from app.utils.ph_clock import ph_now, ph_today  # noqa: F401

def day_bounds(day: date):
    """Start (inclusive) and end (exclusive) of a PH calendar day.

    Trip.departure_time is already PH local, so no offset is applied.
    """
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _trips_for_day(day: date):
    start, end = day_bounds(day)
    return (
        Trip.query.filter(Trip.departure_time >= start, Trip.departure_time < end)
        .order_by(Trip.departure_time)
        .all()
    )


def _passenger_counts(trip_ids):
    """{trip_id: number of manifest entries} in one grouped query."""
    if not trip_ids:
        return {}
    rows = (
        db.session.query(ManifestEntry.trip_id, db.func.count(ManifestEntry.id))
        .filter(ManifestEntry.trip_id.in_(trip_ids))
        .group_by(ManifestEntry.trip_id)
        .all()
    )
    return dict(rows)


def _is_delayed(trip) -> bool:
    return (
        trip.status == "Delayed"
        or trip.delay_reason is not None
        or trip.new_departure_time is not None
    )


def _pct(passengers, capacity):
    if not capacity:  # None (no boat) or 0
        return None
    return round(passengers / capacity * 100, 1)


def get_trip_breakdown(day: date = None):
    """Per-trip rows for the charts. Cancelled trips are excluded."""
    day = day or ph_today()
    trips = [t for t in _trips_for_day(day) if t.status != "Cancelled"]
    counts = _passenger_counts([t.id for t in trips])
    rows = []
    for t in trips:
        capacity = t.boat.capacity if t.boat else None
        passengers = counts.get(t.id, 0)
        rows.append({
            "trip_id": t.id,
            "departure_ph": t.departure_time,
            "boat_name": t.boat.name if t.boat else None,
            "status": t.status,
            "passengers": passengers,
            "capacity": capacity,
            "capacity_pct": _pct(passengers, capacity),
        })
    return rows


def _day_totals(day: date):
    trips = _trips_for_day(day)
    active = [t for t in trips if t.status != "Cancelled"]
    counts = _passenger_counts([t.id for t in active])
    total_passengers = sum(counts.get(t.id, 0) for t in active)
    delayed = sum(1 for t in active if _is_delayed(t))
    return active, counts, total_passengers, delayed


def _change_pct(today_value, yesterday_value):
    """% change vs yesterday, or None when yesterday has no data."""
    if not yesterday_value:
        return None
    return round((today_value - yesterday_value) / yesterday_value * 100)


def get_daily_summary(day: date = None):
    """The four KPI cards."""
    day = day or ph_today()
    active, counts, passengers, delayed = _day_totals(day)
    _, _, y_passengers, y_delayed = _day_totals(day - timedelta(days=1))

    total_seats = sum(t.boat.capacity for t in active if t.boat)
    seated_passengers = sum(counts.get(t.id, 0) for t in active if t.boat)

    # Avg Capacity: only trips that departed or have passengers, and a known capacity
    per_trip = [
        _pct(counts.get(t.id, 0), t.boat.capacity)
        for t in active
        if t.boat and t.boat.capacity
        and (t.status == "Departed" or counts.get(t.id, 0) > 0)
    ]
    avg_capacity = round(sum(per_trip) / len(per_trip), 1) if per_trip else None

    return {
        "day": day,
        "daily_passengers": passengers,
        "passengers_change_pct": _change_pct(passengers, y_passengers),
        "trips_today": len(active),
        "overall_capacity_pct": _pct(seated_passengers, total_seats),
        "avg_capacity_pct": avg_capacity,
        "delayed_trips": delayed,
        "delayed_diff": delayed - y_delayed,
    }


# ---------------------------------------------------------------------------
# Manifest Records (Step 4)
# ---------------------------------------------------------------------------

RECORD_DEFAULT_DAYS = 30   # window used when no date filter is given
MAX_RECORD_ROWS = 200      # safety cap so a wide range cannot load everything
TRIP_STATUSES = ["Open", "Boarding", "Full", "Delayed", "Cancelled", "Departed"]


def _time_label(dt):
    """9:30 AM / 12:00 PM (no leading zero, Windows-safe)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _date_label(d):
    """June 8, 2026 (no leading zero on the day)."""
    return f"{d:%B} {d.day}, {d.year}"


def get_manifest_records(search=None, date_from=None, date_to=None,
                         boat_id=None, status=None, today=None):
    """Trip rows for the Manifest Records table, newest date first.

    - No date filter: the last RECORD_DEFAULT_DAYS days up to today (no future trips).
    - Cancelled trips are included (they are excluded only from the KPIs/charts).
    - search matches passengers (name, passenger type, or exact age) and returns
      the TRIPS that contain at least one match.
    - Within a date, trips are ordered by departure time (earliest first).
    - show_date is True only on the first row of each date, as in the mockup.
    """
    today = today or ph_today()
    if date_from is None and date_to is None:
        date_to = today
        date_from = today - timedelta(days=RECORD_DEFAULT_DAYS - 1)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    query = Trip.query
    if date_from:
        query = query.filter(Trip.departure_time >= day_bounds(date_from)[0])
    if date_to:
        query = query.filter(Trip.departure_time < day_bounds(date_to)[1])
    if boat_id:
        query = query.filter(Trip.boat_id == boat_id)
    if status:
        query = query.filter(Trip.status == status)

    term = (search or "").strip()
    if term:
        like = f"%{term}%"
        conditions = [
            ManifestEntry.full_name.ilike(like),
            ManifestEntry.passenger_type.ilike(like),
        ]
        if term.isdigit():
            conditions.append(ManifestEntry.age == int(term))
        matching_trip_ids = select(ManifestEntry.trip_id).where(db.or_(*conditions))
        query = query.filter(Trip.id.in_(matching_trip_ids))

    # Fetch one extra row so we can tell whether the cap cut anything off.
    trips = query.order_by(Trip.departure_time.desc()).limit(MAX_RECORD_ROWS + 1).all()
    truncated = len(trips) > MAX_RECORD_ROWS
    trips = trips[:MAX_RECORD_ROWS]

    # Newest date first, earliest departure first within a date.
    trips.sort(key=lambda t: (-t.departure_time.date().toordinal(), t.departure_time))

    counts = _passenger_counts([t.id for t in trips])
    rows = []
    previous_day = None
    for t in trips:
        day = t.departure_time.date()
        rows.append({
            "trip_id": t.id,
            "date": day,
            "date_label": _date_label(day),
            "show_date": day != previous_day,
            "time_label": _time_label(t.departure_time),
            "boat_name": t.boat.name if t.boat else None,
            "status": t.status,
            "passengers": counts.get(t.id, 0),
            "capacity": t.boat.capacity if t.boat else None,
        })
        previous_day = day

    return {"rows": rows, "truncated": truncated,
            "date_from": date_from, "date_to": date_to}


# ---------------------------------------------------------------------------
# Manifest Details (Step 5)
# ---------------------------------------------------------------------------

def get_manifest_detail(trip_id):
    """Everything the Manifest Details card needs for one trip, or None.

    The passenger list is always the FULL manifest (never the searched subset),
    ordered by check-in time and numbered 1..n. "Driver's Name" in the mockup
    is Trip.crew_name (there is no separate driver field).
    """
    trip = db.session.get(Trip, trip_id)
    if trip is None:
        return None

    entries = (
        ManifestEntry.query.filter_by(trip_id=trip.id)
        .order_by(ManifestEntry.check_in_time, ManifestEntry.id)
        .all()
    )
    passenger_list = [
        {
            "no": number,
            "full_name": e.full_name,
            "age": e.age,
            "address": e.address,
            "passenger_type": e.passenger_type,
            "check_in_label": e.check_in_time.strftime("%H:%M:%S") if e.check_in_time else "—",
        }
        for number, e in enumerate(entries, start=1)
    ]
    return {
        "trip_id": trip.id,
        "date_label": _date_label(trip.departure_time.date()),
        "time_label": _time_label(trip.departure_time),
        "boat_name": trip.boat.name if trip.boat else None,
        "driver_name": trip.crew_name,
        "capacity": trip.boat.capacity if trip.boat else None,
        "passengers": len(entries),
        "status": trip.status,
        "passenger_list": passenger_list,
    }