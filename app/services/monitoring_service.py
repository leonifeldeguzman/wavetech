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
from app.services import llda_service, windy_service
from app.services.llda_service import LLDAServiceError
from app.services.windy_service import WindyServiceError
from app.utils.timezone import to_naive_utc

STATUS_LIVE = "live"
STATUS_CACHED = "cached"
STATUS_UNAVAILABLE = "unavailable"


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