"""Historical Baseline: seasonal context from the LLDA daily lake-level data.

This is lake-wide REFERENCE data. It never produces a current water level
and must never be shown as one. It only answers: "for this time of year,
what range of levels has been seen before?"
"""
import math
from datetime import date, timedelta

from sqlalchemy import func, or_

from app.extensions import db
from app.models.historical_water_level import HistoricalWaterLevel as H
from datetime import datetime, timedelta, timezone

from app.models.environmental_reading import EnvironmentalReading
from app.utils.ph_clock import ph_today 

# Days either side of the calendar day, pooled across every year in the data.
BASELINE_WINDOW_DAYS = 7
# Below this many samples we return None rather than a shaky band.
MIN_SAMPLES = 20
PERCENTILES = (10, 25, 50, 75, 90)


def _percentile(sorted_values, p):
    """Linear-interpolated percentile of an already sorted list."""
    k = (len(sorted_values) - 1) * p / 100
    lo = math.floor(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _same_day_in_year(day, year):
    """The same month/day in another year (Feb 29 falls back to Feb 28)."""
    try:
        return date(year, day.month, day.day)
    except ValueError:
        return date(year, 2, 28)


def get_baseline_for_day(day, window_days=BASELINE_WINDOW_DAYS):
    """Seasonal range for the calendar day of `day` (the year is ignored).

    Returns a dict, or None if there is no data / too few samples:
      day, window_days, n, years, label,
      min, p10, p25, p50, p75, p90, max   (meters, rounded to 3 decimals)
    """
    first, last = db.session.query(func.min(H.date), func.max(H.date)).one()
    if first is None:
        return None

    delta = timedelta(days=window_days)
    clauses = []
    for year in range(first.year, last.year + 1):
        center = _same_day_in_year(day, year)
        clauses.append(H.date.between(center - delta, center + delta))

    values = sorted(r[0] for r in db.session.query(H.level_m).filter(or_(*clauses)))
    if len(values) < MIN_SAMPLES:
        return None

    result = {
        "day": day,
        "window_days": window_days,
        "n": len(values),
        "years": list(range(first.year, last.year + 1)),
        "label": (f"Historical baseline: LLDA lake-wide daily data, "
                  f"{first.year}-{last.year}. Not a current reading."),
        "min": round(values[0], 3),
        "max": round(values[-1], 3),
    }
    for p in PERCENTILES:
        result[f"p{p}"] = round(_percentile(values, p), 3)
    return result


def classify_level(level_m, baseline):
    """Place a CURRENT, measured level (live LLDA or a manual entry) against
    the baseline. Returns {"band", "text"} or None.

    `level_m` must come from a real current source. Never pass a historical
    value in as if it were current.
    """
    if level_m is None or baseline is None:
        return None
    if level_m < baseline["min"]:
        band, text = "below_range", "Below the range seen at this time of year"
    elif level_m < baseline["p10"]:
        band, text = "low", "Low for this time of year"
    elif level_m <= baseline["p90"]:
        band, text = "typical", "Within the usual range for this time of year"
    elif level_m <= baseline["max"]:
        band, text = "high", "High for this time of year"
    else:
        band, text = "above_range", "Above the range seen at this time of year"
    return {"band": band, "text": text}


MAX_READING_AGE = timedelta(hours=24)


def get_level_context():
    """Compare the newest real (manual or LLDA) water level with the
    historical baseline for today (PH date).

    Advisory only: never feeds the recommendation, never uses mock data.
    state is one of: "no_baseline", "no_reading", "stale", "ok".
    """
    baseline = get_baseline_for_day(ph_today())
    if baseline is None:
        return {"state": "no_baseline", "baseline": None, "reading": None, "context": None}

    reading = (
        EnvironmentalReading.query
        .filter(
            EnvironmentalReading.water_level_m.isnot(None),
            EnvironmentalReading.source.in_(("manual", "llda")),
        )
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )
    if reading is None or reading.retrieved_at is None:
        return {"state": "no_reading", "baseline": baseline, "reading": None, "context": None}

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    age = now_utc - reading.retrieved_at
    if age < timedelta(0) or age > MAX_READING_AGE:
        return {"state": "stale", "baseline": baseline, "reading": reading, "context": None}

    context = classify_level(reading.water_level_m, baseline)  # ADAPT if the signature differs
    return {"state": "ok", "baseline": baseline, "reading": reading, "context": context}