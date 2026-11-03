"""Analytics queries for Reports & Analytics (read-only, no UI).

All "today" logic uses the Philippine calendar day.
Trip.departure_time is stored as naive PH wall-clock time (confirmed in
flask shell: a 9:30 AM slot is stored as 09:30). The current moment, however,
comes from the server clock, which is UTC on cloud hosts such as Vercel, so
ph_today() (app/utils/ph_clock.py) converts.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, not_, select

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




LIVE_STATUSES = ("Open", "Boarding", "Full", "Delayed")
COUNTED_STATUSES = LIVE_STATUSES + ("Departed",)
SUMMARY_ORDER = ("Open", "Full", "Delayed", "Departed", "Cancelled")


def _effective_departure(trip):
    """A Delayed trip counts at its new time, everything else at its slot time."""
    if trip.status == "Delayed" and trip.new_departure_time:
        return trip.new_departure_time
    return trip.departure_time


def _real_trips(day: date):
    """Trips that really exist on a day: a boat is assigned, and the slot
    is not expired (past and never departed). Cancelled trips are kept."""
    now = ph_now()
    trips = []
    for t in _trips_for_day(day):
        if t.boat is None:
            continue  # unassigned slot
        if t.status in LIVE_STATUSES and _effective_departure(t) < now:
            continue  # expired slot
        if t.status in COUNTED_STATUSES or t.status == "Cancelled":
            trips.append(t)
    return trips


def _status_counts(trips):
    """{'Open': n, 'Full': n, ...}. Boarding is shown as Open."""
    result = {s: 0 for s in SUMMARY_ORDER}
    for t in trips:
        label = "Open" if t.status == "Boarding" else t.status
        result[label] += 1
    return result


def _day_totals(day: date):
    real = _real_trips(day)
    counted = [t for t in real if t.status != "Cancelled"]
    counts = _passenger_counts([t.id for t in counted])
    total_passengers = sum(counts.get(t.id, 0) for t in counted)
    return real, counted, counts, total_passengers

def get_flow_chart(day: date = None):
    """One entry per trip for the Passenger Flow bar chart, sorted by time.

    Open, Full, Delayed (at the new time) and Departed only.
    Cancelled, unassigned and expired trips are left out.
    """
    day = day or ph_today()
    _, counted, counts, _ = _day_totals(day)
    rows = []
    for t in sorted(counted, key=_effective_departure):
        rows.append({
            "trip_id": t.id,
            "label": f"{_time_label(_effective_departure(t))}, {t.boat.name}",
            "passengers": counts.get(t.id, 0),
            "status": t.status,
        })
    return rows


def get_capacity_chart(day: date = None):
    """One entry per trip for the Capacity Utilization bars, sorted by time.

    Same trips as the Passenger Flow chart: Open, Full, Delayed (at the new
    time) and Departed. Cancelled, unassigned and expired trips are left out.
    """
    day = day or ph_today()
    _, counted, counts, _ = _day_totals(day)
    rows = []
    for t in sorted(counted, key=_effective_departure):
        capacity = t.boat.capacity
        passengers = counts.get(t.id, 0)
        pct = _pct(passengers, capacity)
        if pct is None:
            count_label = f"{passengers} / —"
        else:
            count_label = f"{passengers} / {capacity}, {pct:.0f}%"
        rows.append({
            "trip_id": t.id,
            "label": f"{_time_label(_effective_departure(t))}, {t.boat.name}",
            "passengers": passengers,
            "capacity": capacity,
            "capacity_pct": pct,
            "count_label": count_label,
            "status": t.status,
            "is_full": t.status == "Full",
        })
    return rows

TREND_DAYS = 7


def get_trend_chart(day: date = None):
    """One point per day for the last 7 days, oldest first.

    A day with no counted trips has passengers=None (shown as a gap, not zero).
    A day with counted trips but no passengers is a real 0.
    """
    day = day or ph_today()
    rows = []
    for offset in range(TREND_DAYS - 1, -1, -1):
        d = day - timedelta(days=offset)
        _, counted, _, passengers = _day_totals(d)
        rows.append({
            "date": d,
            "label": f"{d:%b} {d.day}",
            "passengers": passengers if counted else None,
        })
    return rows

def get_daily_summary(day: date = None):
    """The four KPI cards."""
    day = day or ph_today()
    real, counted, counts, passengers = _day_totals(day)
    y_real, y_counted, _, y_passengers = _day_totals(day - timedelta(days=1))

    status_counts = _status_counts(real)
    trips_caption = ", ".join(
        f"{s} {status_counts[s]}" for s in SUMMARY_ORDER if status_counts[s]
    )

    # Avg Capacity: every counted trip that has a known capacity
    per_trip = [
        _pct(counts.get(t.id, 0), t.boat.capacity)
        for t in counted
        if t.boat.capacity
    ]
    avg_capacity = round(sum(per_trip) / len(per_trip), 1) if per_trip else None

    total_seats = sum(t.boat.capacity for t in counted if t.boat.capacity)
    y_delayed = _status_counts(y_real)["Delayed"]

    return {
        "day": day,
        "daily_passengers": passengers,
        "yesterday_passengers": y_passengers if y_counted else None,
        "passengers_change_pct": _change_pct(passengers, y_passengers),
        "trips_today": len(real),
        "trips_caption": trips_caption,
        "overall_capacity_pct": _pct(passengers, total_seats),
        "avg_capacity_pct": avg_capacity,
        "departed_trips": status_counts["Departed"],
        "delayed_trips": status_counts["Delayed"],
        "cancelled_trips": status_counts["Cancelled"],
        "delayed_diff": status_counts["Delayed"] - y_delayed,
    }



def _change_pct(today_value, yesterday_value):
    """% change vs yesterday, or None when yesterday has no data."""
    if not yesterday_value:
        return None
    return round((today_value - yesterday_value) / yesterday_value * 100)





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

def _expired_condition():
    """SQL version of 'expired': still live, and past its effective time.

    A Delayed trip is judged by its new departure time, like in _real_trips().
    """
    effective = case(
        (and_(Trip.status == "Delayed", Trip.new_departure_time.isnot(None)),
         Trip.new_departure_time),
        else_=Trip.departure_time,
    )
    return and_(Trip.status.in_(LIVE_STATUSES), effective < ph_now())

def get_manifest_records(search=None, date_from=None, date_to=None,
                         boat_id=None, status=None, today=None):
    """Trip rows for the Manifest Records table, newest date first.

    - No date filter: the last RECORD_DEFAULT_DAYS days up to today (no future trips).
    - Cancelled trips are included (they are excluded only from the KPIs/charts).
    - search matches passengers (name, passenger type, or exact age) and returns
      the TRIPS that contain at least one match.
    - Within a date, trips are ordered by departure time (earliest first).
    - show_date is True only on the first row of each date, as in the mockup.
    - Unassigned (no boat) and expired trips are hidden.
    """
    today = today or ph_today()
    if date_from is None and date_to is None:
        date_to = today
        date_from = today - timedelta(days=RECORD_DEFAULT_DAYS - 1)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    # Hide unassigned slots (no boat) and expired slots (past time, never departed).
    query = Trip.query.filter(Trip.boat_id.isnot(None), not_(_expired_condition()))
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