"""
Orchestrates the live-data flow described in the SRS for BOTH environmental
sources feeding the Lake Monitoring dashboard:

* LLDA -> water level          (source="llda")
* Windy Point Forecast -> wind speed/direction, weather, temperature (source="windy")

    <source> API request
           |
       Successful?
       /        \\
     YES          NO
      |            |
  Save data   Retrieve latest cached data (same source)
                    |
             Cached data available?
                /          \\
              YES           NO
               |             |
        Display cached   Display degraded
        data + timestamp  monitoring warning

Each source is fetched and evaluated independently, so e.g. LLDA being
unavailable does not affect the Windy card and vice versa. Keeping this
decision here (rather than in routes/templates) is what lets
`no_data_blocks_recommendation()` guarantee that stale/missing data can
never silently feed a future scheduling recommendation.
"""
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from sqlalchemy import func
from app.models.lake_station import LakeStation
from app.services import llda_service, settings_service, weather_service, windy_service
from app.services.weather_service import WeatherServiceError
from app.services.llda_service import LLDAServiceError
from app.services.windy_service import WindyServiceError
from app.utils.ph_clock import PH_OFFSET, ph_today
from app.utils.timezone import to_naive_utc
from datetime import datetime, timedelta, timezone

STATUS_LIVE = "live"
STATUS_CACHED = "cached"
STATUS_UNAVAILABLE = "unavailable"
STATUS_MANUAL = "manual"


def _fresh_reading(source: str):
    """Return the latest reading when it is within the configured refresh window."""
    cached = _latest_reading(source)
    if cached is None or cached.retrieved_at is None:
        return None

    cached_time = cached.retrieved_at
    if cached_time.tzinfo is None:
        cached_time = cached_time.replace(tzinfo=timezone.utc)
    else:
        cached_time = cached_time.astimezone(timezone.utc)

    delta = datetime.now(timezone.utc) - cached_time
    refresh_interval = timedelta(seconds=settings_service.get_data_cache_ttl_seconds())
    if timedelta(0) <= delta < refresh_interval:
        return cached
    return None


def _latest_reading(source: str):
    return (
        EnvironmentalReading.query.filter_by(source=source)
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )


def _fallback(source: str, error: Exception) -> dict:
    cached = _latest_reading(source)
    if cached is not None:
        return {"status": STATUS_CACHED, "reading": cached, "message": str(error)}
    return {
        "status": STATUS_UNAVAILABLE,
        "reading": None,
        "message": "Manual verification is required.",
    }


def get_llda_conditions() -> dict:
    """Attempt a live LLDA fetch; fall back to cached, then to unavailable.

    Returns {"status": "live"|"cached"|"unavailable", "reading": EnvironmentalReading|None, "message": str|None}
    """
    cached = _fresh_reading("llda")
    if cached is not None:
        return {"status": STATUS_LIVE, "reading": cached, "message": None}

    try:
        live = llda_service.fetch_water_level()
    except LLDAServiceError as exc:
        return _fallback("llda", exc)

    reading = EnvironmentalReading(
        source="llda",
        location_label=live.station,
        water_level_m=live.water_level_m,
        recorded_at=to_naive_utc(live.recorded_at),
    )
    db.session.add(reading)
    db.session.commit()

    return {"status": STATUS_LIVE, "reading": reading, "message": None}


def get_windy_conditions() -> dict:
    """Attempt a live Windy fetch; fall back to cached, then to unavailable.

    Same shape as get_llda_conditions(), but for wind/weather/temperature.
    """
    cached = _fresh_reading("windy")
    if cached is not None:
        return {"status": STATUS_LIVE, "reading": cached, "message": None}

    try:
        live = windy_service.fetch_conditions()
    except WindyServiceError as exc:
        return _fallback("windy", exc)

    reading = EnvironmentalReading(
        source="windy",
        wind_speed_kmh=live.wind_speed_kmh,
        wind_direction=live.wind_direction,
        weather_condition=live.weather_condition,
        temperature_c=live.temperature_c,
        recorded_at=to_naive_utc(live.recorded_at),
        retrieved_at=to_naive_utc(live.retrieved_at),
    )
    db.session.add(reading)
    db.session.commit()

    return {"status": STATUS_LIVE, "reading": reading, "message": None}

SOURCE_OPEN_METEO = "open_meteo"
SOURCE_MANUAL = "manual"

# How long a manually entered weather observation may stand in for the
# weather model when Open-Meteo is cached or unavailable. ADVISER: confirm.
MANUAL_WEATHER_MAX_AGE = timedelta(hours=3)


def _is_fresh(retrieved_at) -> bool:
    """True if a stored naive-UTC timestamp is inside the refresh window.
    Rejects a negative age, like _fresh_reading()."""
    if retrieved_at is None:
        return False
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now_utc - retrieved_at
    interval = timedelta(seconds=settings_service.get_data_cache_ttl_seconds())
    return timedelta(0) <= delta < interval


def _latest_station_batch() -> list:
    """Rows from the newest Open-Meteo fetch. One fetch inserts one row per
    station, all with the same retrieved_at, so that timestamp identifies
    the batch."""
    newest = (
        db.session.query(func.max(EnvironmentalReading.retrieved_at))
        .filter(
            EnvironmentalReading.source == SOURCE_OPEN_METEO,
            EnvironmentalReading.station_id.isnot(None),
        )
        .scalar()
    )
    if newest is None:
        return []
    return (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.source == SOURCE_OPEN_METEO,
            EnvironmentalReading.station_id.isnot(None),
            EnvironmentalReading.retrieved_at == newest,
        )
        .all()
    )


def _pair(stations, readings) -> list:
    by_station = {r.station_id: r for r in readings}
    return [{"station": s, "reading": by_station.get(s.id)} for s in stations]


def get_station_weather() -> dict:
    """Per-station weather from Open-Meteo (a weather-MODEL proxy, not a lake
    measurement). Same live/cached/unavailable cycle as the other sources.

    Returns {"status", "stations": [{"station", "reading"}], "message"}.
    Water level is never stored here: it is lake-wide.
    """
    stations = LakeStation.query.order_by(LakeStation.id).all()
    if not stations:
        return {"status": STATUS_UNAVAILABLE, "stations": [],
                "message": "No stations are configured."}

    batch = _latest_station_batch()
    covered = {r.station_id for r in batch}
    if batch and all(s.id in covered for s in stations) and _is_fresh(batch[0].retrieved_at):
        return {"status": STATUS_LIVE, "stations": _pair(stations, batch), "message": None}

    try:
        fetched = weather_service.fetch_station_weather(stations)
    except WeatherServiceError as exc:
        if batch:
            return {"status": STATUS_CACHED, "stations": _pair(stations, batch),
                    "message": str(exc)}
        return {"status": STATUS_UNAVAILABLE, "stations": _pair(stations, []),
                "message": "Weather data is unavailable. Manual verification is required."}

    by_no = {s.station_no: s for s in stations}
    rows = []
    for w in fetched:
        station = by_no[w.station_no]
        rows.append(EnvironmentalReading(
            source=SOURCE_OPEN_METEO,
            station_id=station.id,
            location_label=f"{station.name} (Station {station.station_no})",
            wind_speed_kmh=w.wind_speed_kmh,
            wind_direction=w.wind_direction,
            weather_condition=w.weather_condition,
            temperature_c=w.temperature_c,
            recorded_at=w.recorded_at,
            retrieved_at=w.retrieved_at,
        ))
    db.session.add_all(rows)
    db.session.commit()

    return {"status": STATUS_LIVE, "stations": _pair(stations, rows), "message": None}


# Station whose weather feeds Scheduling Decision-Support.
# ASSUMPTION: Cardona (XXI) is the station nearest the ferry route.
# Confirm with the adviser/LLDA.
DSS_STATION_NO = "XXI"


def pick_reference_reading(station_weather: dict):
    """The reading for the reference station (DSS_STATION_NO) out of a
    get_station_weather() result, or None. Admin and Passenger both use
    this, so the headline weather is always the same observation."""
    for item in station_weather["stations"]:
        if item["station"].station_no == DSS_STATION_NO:
            return item["reading"]
    return None

def get_recent_manual_weather():
    """Newest manual weather entry for the reference station (DSS_STATION_NO),
    or None if there is none or it is older than MANUAL_WEATHER_MAX_AGE.
    Needs both wind speed and condition, so it can be classified."""
    station = LakeStation.query.filter_by(station_no=DSS_STATION_NO).first()
    if station is None:
        return None
    reading = (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.source == SOURCE_MANUAL,
            EnvironmentalReading.station_id == station.id,
            EnvironmentalReading.wind_speed_kmh.isnot(None),
            EnvironmentalReading.weather_condition.isnot(None),
        )
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )
    if reading is None or reading.retrieved_at is None:
        return None
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    age = now_utc - reading.retrieved_at
    # Reject a negative age, like _is_fresh().
    if timedelta(0) <= age <= MANUAL_WEATHER_MAX_AGE:
        return reading
    return None


def get_dss_weather_conditions(station_weather: dict | None = None) -> dict:
    """Open-Meteo weather for the representative station, for
    decision-support. Same shape as get_windy_conditions():
    {"status", "reading", "message"}. Weather-model data, not a lake
    measurement."""
    result = station_weather if station_weather is not None else get_station_weather()
    reading = pick_reference_reading(result)

    # Open-Meteo wins whenever it is live. Otherwise a recent manual entry
    # may stand in, and it keeps its own "manual" status so it is never
    # shown as weather-model data.
    if result["status"] != STATUS_LIVE:
        manual = get_recent_manual_weather()
        if manual is not None:
            return {
                "status": STATUS_MANUAL,
                "reading": manual,
                "message": "Weather entered manually by the operator.",
            }

    if reading is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reading": None,
            "message": "Weather data is unavailable. Manual verification is required.",
        }
    return {
        "status": result["status"],
        "reading": reading,
        "message": result["message"],
    }


def no_data_blocks_recommendation(*conditions: dict) -> bool:
    """True if any of the given `conditions` dicts is NOT good enough to
    base a scheduling recommendation on.

    Per the SRS: the system must never generate a Proceed/Delay/Suspend
    recommendation from outdated or unavailable environmental data. Only
    a LIVE reading from every required source may feed that logic; CACHED
    and UNAVAILABLE must not, even though CACHED is still shown to the
    operator for situational awareness. (Scheduling recommendations
    themselves are a separate, not-yet-built feature — this guard exists
    so that future work wires into the same rule instead of re-deciding
    it.)
    """
    return any(c.get("status") != STATUS_LIVE for c in conditions)


# ---------------------------------------------------------------------------
# Readings shown on BOTH Admin (Monitoring page) and Passenger (home page).
# Moved unchanged from monitoring/routes.py so both sides share one source.
# ---------------------------------------------------------------------------

def get_latest_reading():
    """Newest reading of any visible source. Rows from the per-station weather
    proxy (open_meteo) and old Windy test rows are not shown."""
    hidden_sources = (SOURCE_OPEN_METEO, "windy")
    return (
        EnvironmentalReading.query.filter(
            EnvironmentalReading.source.notin_(hidden_sources)
        )
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )


def get_latest_water_reading():
    """Newest water level from a real current source (LLDA or manual).
    No freshness cut-off here: age is shown on the card instead."""
    return (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.water_level_m.isnot(None),
            EnvironmentalReading.source.in_(("llda", "manual")),
        )
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )

def get_todays_observation():
    """Newest manual row carrying an operator observation, accepted only if
    recorded today (PH calendar day). Older or missing means None."""
    reading = (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.source == SOURCE_MANUAL,
            EnvironmentalReading.observed_condition.isnot(None),
        )
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )
    if reading is None or reading.retrieved_at is None:
        return None
    # retrieved_at is naive UTC; convert to the PH calendar day.
    taken_ph = reading.retrieved_at + timedelta(hours=8)
    if taken_ph.date() != ph_today():
        return None
    return reading


def build_water_level_card(reading):
    """Admin's Water Level card data: the reading plus its age."""
    card = {"reading": None, "age_days": None,
            "is_today": False, "date_label": None}
    if reading and reading.retrieved_at:
        taken_ph = reading.retrieved_at + timedelta(hours=8)
        age_days = max((ph_today() - taken_ph.date()).days, 0)
        card = {
            "reading": reading,
            "age_days": age_days,
            "is_today": age_days == 0,
            "date_label": taken_ph.strftime("%b %d, %Y"),
        }
    return card


def classify_water_safety(water_level_m, thresholds=None):
    """Classify a water level against the Safety Thresholds saved in Admin
    Settings. Returns (safety_status, safety_label).

    Uses all four saved water-level values (settings_service
    .get_safety_thresholds()), with the same inclusive boundaries as
    Scheduling Decision-Support:
        safe_min <= level <= safe_max          -> "safe"
        caution_min <= level <= caution_max    -> "caution"
        anything outside the caution range     -> "unsafe"
    Nothing is hardcoded: change the values in Settings and this follows.
    """
    if thresholds is None:
        thresholds = settings_service.get_safety_thresholds()
    safe_min = float(thresholds["water_safe_min_m"])
    safe_max = float(thresholds["water_safe_max_m"])
    caution_min = float(thresholds["water_caution_min_m"])
    caution_max = float(thresholds["water_caution_max_m"])

    level = float(water_level_m)
    if safe_min <= level <= safe_max:
        return "safe", "SAFE"
    if caution_min <= level <= caution_max:
        return "caution", "CAUTION"
    return "unsafe", "UNSAFE"


def get_current_safety(assessment=None):
    """Overall Safety Status shown on the Admin dashboard and Passenger home.

    This is the Scheduling Decision-Support overall condition, normalized to
    ("safe" | "caution" | "unsafe" | "unavailable", "SAFE" | ... ), so the
    dashboard, Passenger home and DSS can never disagree for the same data.
    There is no second calculation here: water level (current day only),
    wind and weather are all judged in scheduling_service.evaluate_conditions()
    against the Safety Thresholds saved in Admin Settings, and missing,
    stale or unrecognised required data is "unavailable", never "caution".
    Advisory only: the Coast Guard remains the final authority.

    Pass the `assessment` you already computed (the dashboard does) to
    reuse it; otherwise the current one is evaluated.
    Returns (safety_status, safety_label, latest_water_reading); the reading
    is the newest stored water level, kept only for the "Last check" time.
    """
    # Imported here: scheduling_service imports this module.
    from app.services import scheduling_service

    if assessment is None:
        assessment = scheduling_service.get_assessment()

    safety_status = assessment.overall_status.lower()
    safety_label = assessment.overall_status.upper()

    latest_reading = (
        EnvironmentalReading.query
        .filter(EnvironmentalReading.water_level_m.isnot(None))
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )
    return safety_status, safety_label, latest_reading


# ---------------------------------------------------------------------------
# Station weather history for the Trends chart (Open-Meteo model data)
# ---------------------------------------------------------------------------

HISTORY_DEFAULT_DAYS = 7
HISTORY_MAX_DAYS = 31
GAP_HOURS = 3  # a longer silence between stored points is drawn as a gap


def _chart_ms(utc_naive):
    """Naive UTC datetime -> milliseconds of the PH wall-clock time, read as
    if it were UTC. The chart prints it back with UTC formatting, so the
    axis shows Philippine time."""
    return int((utc_naive + PH_OFFSET).replace(tzinfo=timezone.utc).timestamp() * 1000)


def get_station_history(station_no=None, start=None, end=None):
    """Stored Open-Meteo weather for ONE station over a PH date range.

    Only rows WaveTech actually stored are returned. A silence longer than
    GAP_HOURS becomes a break in the line (a None point); nothing is
    interpolated or filled in. Values are weather-MODEL data for a grid
    cell, not lake measurements.
    Returns None if no stations are configured.
    """
    stations = LakeStation.query.order_by(LakeStation.id).all()
    if not stations:
        return None
    by_no = {s.station_no: s for s in stations}
    by_id = {s.id: s for s in stations}
    station = by_no.get(station_no) or by_no.get(DSS_STATION_NO) or stations[0]

    today = ph_today()
    end = today if end is None else min(end, today)
    start = end - timedelta(days=HISTORY_DEFAULT_DAYS - 1) if start is None else start
    if start > end:
        start = end
    if (end - start).days >= HISTORY_MAX_DAYS:
        start = end - timedelta(days=HISTORY_MAX_DAYS - 1)

    # A PH day is [00:00 PH, next 00:00 PH); stored times are naive UTC.
    lo = datetime.combine(start, datetime.min.time()) - PH_OFFSET
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time()) - PH_OFFSET

    rows = (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.source == SOURCE_OPEN_METEO,
            EnvironmentalReading.station_id == station.id,
            EnvironmentalReading.recorded_at.isnot(None),
            EnvironmentalReading.recorded_at >= lo,
            EnvironmentalReading.recorded_at < hi,
        )
        .order_by(EnvironmentalReading.recorded_at, EnvironmentalReading.retrieved_at)
        .all()
    )

    # Several fetches can return the same model time; keep one point each.
    by_time = {}
    for r in rows:
        by_time[r.recorded_at] = r
    times = sorted(by_time)

    wind, temp = [], []
    gaps = 0
    prev = None
    for t in times:
        r = by_time[t]
        if prev is not None and (t - prev) > timedelta(hours=GAP_HOURS):
            mid = _chart_ms(prev + (t - prev) / 2)
            wind.append({"x": mid, "y": None})
            temp.append({"x": mid, "y": None})
            gaps += 1
        wind.append({"x": _chart_ms(t), "y": r.wind_speed_kmh})
        temp.append({"x": _chart_ms(t), "y": r.temperature_c})
        prev = t

    # Which other stations show identical values right now (same model
    # cell). Same rule the station cards use.
    shared_with = []
    batch = _latest_station_batch()
    mine = next((r for r in batch if r.station_id == station.id), None)
    if mine is not None:
        def _key(r):
            return (r.wind_speed_kmh, r.wind_direction,
                    r.temperature_c, r.weather_condition)
        shared_with = [
            by_id[r.station_id].name for r in batch
            if r.station_id != station.id
            and r.station_id in by_id
            and _key(r) == _key(mine)
        ]

    # Strongest model wind at any station in the last 24 hours. Ties (stations
    # in one cell share a value) are all named, never one picked as winner.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    top = (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.source == SOURCE_OPEN_METEO,
            EnvironmentalReading.station_id.isnot(None),
            EnvironmentalReading.wind_speed_kmh.isnot(None),
            EnvironmentalReading.recorded_at >= now_utc - timedelta(hours=24),
        )
        .order_by(EnvironmentalReading.wind_speed_kmh.desc(),
                  EnvironmentalReading.recorded_at.desc())
        .first()
    )
    strongest_wind = None
    if top is not None:
        tied = (
            EnvironmentalReading.query
            .filter(
                EnvironmentalReading.source == SOURCE_OPEN_METEO,
                EnvironmentalReading.station_id.isnot(None),
                EnvironmentalReading.wind_speed_kmh == top.wind_speed_kmh,
                EnvironmentalReading.recorded_at == top.recorded_at,
            )
            .all()
        )
        strongest_wind = {
            "speed": top.wind_speed_kmh,
            "direction": top.wind_direction,
            "at": top.recorded_at + PH_OFFSET,
            "stations": sorted({by_id[r.station_id].name
                                for r in tied if r.station_id in by_id}),
        }

    return {
        "stations": stations,
        "station": station,
        "start": start,
        "end": end,
        "x_min": _chart_ms(lo),
        "x_max": _chart_ms(hi),
        "wind": wind,
        "temp": temp,
        "n_points": len(times),
        "n_gaps": gaps,
        "first_point": (times[0] + PH_OFFSET) if times else None,
        "shared_with": shared_with,
        "strongest_wind": strongest_wind,
    }