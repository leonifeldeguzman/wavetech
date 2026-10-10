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
from app.utils.ph_clock import ph_today
from app.utils.timezone import to_naive_utc
from datetime import datetime, timedelta, timezone

STATUS_LIVE = "live"
STATUS_CACHED = "cached"
STATUS_UNAVAILABLE = "unavailable"


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
    refresh_interval = timedelta(seconds=settings_service.get_refresh_interval_seconds())
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


def _is_fresh(retrieved_at) -> bool:
    """True if a stored naive-UTC timestamp is inside the refresh window.
    Rejects a negative age, like _fresh_reading()."""
    if retrieved_at is None:
        return False
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now_utc - retrieved_at
    interval = timedelta(seconds=settings_service.get_refresh_interval_seconds())
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


def get_dss_weather_conditions() -> dict:
    """Open-Meteo weather for the representative station, for
    decision-support. Same shape as get_windy_conditions():
    {"status", "reading", "message"}. Weather-model data, not a lake
    measurement."""
    result = get_station_weather()

    reading = None
    for item in result["stations"]:
        if item["station"].station_no == DSS_STATION_NO:
            reading = item["reading"]
            break

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
    """Newest reading of any visible source. Admin's "Wave Activity" card
    reads wave_height_m from this row. Rows from the per-station weather
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


def get_current_safety():
    """Safety Status shown on Admin dashboard and Passenger home.

    The low/high water-level limits are the Water Level safe range saved in
    Admin Settings (settings_service.get_safety_thresholds()), the same
    values Scheduling Decision-Support reads, so a change saved in Settings
    applies here immediately. Nothing is hardcoded.
    Returns (safety_status, safety_label, latest_reading)."""
    thresholds = settings_service.get_safety_thresholds()
    water_low = float(thresholds["water_safe_min_m"])
    water_high = float(thresholds["water_safe_max_m"])

    latest_reading = (
        EnvironmentalReading.query
        .filter(EnvironmentalReading.water_level_m.isnot(None))
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )

    if latest_reading is None:
        safety_status = "pending"
        safety_label = "Pending Setup"
    elif latest_reading.water_level_m < water_low:
        safety_status = "critical-low"
        safety_label = "CRITICAL LOW"
    elif latest_reading.water_level_m > water_high:
        safety_status = "critical-high"
        safety_label = "CRITICAL HIGH"
    else:
        safety_status = "clear"
        safety_label = "CLEAR"

    return safety_status, safety_label, latest_reading