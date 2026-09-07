"""
Orchestrates the LLDA live-data flow described in the SRS:

    LLDA API request
           |
       Successful?
       /        \\
     YES          NO
      |            |
  Save data   Retrieve latest cached data
                    |
             Cached data available?
                /          \\
              YES           NO
               |             |
        Display cached   Display degraded
        data + timestamp  monitoring warning

This module is the only place that decides whether the dashboard shows
LIVE, CACHED, or UNAVAILABLE — routes and templates just render whatever
it returns. Keeping that decision in one place is what lets
`no_data_blocks_recommendation()` guarantee that stale/missing data can
never silently feed a scheduling recommendation.
"""
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import llda_service
from app.services.llda_service import LLDAServiceError

STATUS_LIVE = "live"
STATUS_CACHED = "cached"
STATUS_UNAVAILABLE = "unavailable"


def _latest_llda_reading():
    return (
        EnvironmentalReading.query.filter_by(source="llda")
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )


def get_llda_conditions():
    """Attempt a live LLDA fetch; fall back to cached, then to unavailable.

    Returns a dict:
        {
            "status": "live" | "cached" | "unavailable",
            "reading": EnvironmentalReading | None,
            "message": str | None,   # explanation, mainly for cached/unavailable
        }
    """
    try:
        live = llda_service.fetch_water_level()
    except LLDAServiceError as exc:
        cached = _latest_llda_reading()
        if cached is not None:
            return {
                "status": STATUS_CACHED,
                "reading": cached,
                "message": str(exc),
            }
        return {
            "status": STATUS_UNAVAILABLE,
            "reading": None,
            "message": "Manual verification is required.",
        }

    reading = EnvironmentalReading(
        source="llda",
        location_label=live.station,
        water_level_m=live.water_level_m,
        recorded_at=live.recorded_at,
        retrieved_at=live.retrieved_at,
    )
    db.session.add(reading)
    db.session.commit()

    return {
        "status": STATUS_LIVE,
        "reading": reading,
        "message": None,
    }


def no_data_blocks_recommendation(conditions: dict) -> bool:
    """True if `conditions` is NOT good enough to base a scheduling
    recommendation on.

    Per the SRS: the system must never generate a Proceed/Delay/Suspend
    recommendation from outdated or unavailable environmental data. Only
    a LIVE reading may feed that logic; CACHED and UNAVAILABLE must not,
    even though CACHED is still shown to the operator for situational
    awareness. (Scheduling recommendations themselves are a separate,
    not-yet-built feature — this guard exists so that future work wires
    into the same rule instead of re-deciding it.)
    """
    return conditions.get("status") != STATUS_LIVE