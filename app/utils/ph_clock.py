"""Philippine-time helpers (app/utils/ph_clock.py).

The Philippines has no daylight saving, so a fixed UTC+8 offset is exact.

Cloud hosts run their clock in UTC, but WaveTech stores trip times and
check-in times as naive PH wall-clock time. Use these helpers instead of
datetime.now(), date.today() or db.func.now() whenever the value is compared
with, or stored next to, PH-local data.
"""
from datetime import date, datetime, timedelta, timezone

PH_OFFSET = timedelta(hours=8)


def ph_now() -> datetime:
    """Current PH wall-clock time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + PH_OFFSET


def ph_today() -> date:
    """Current calendar date in the Philippines."""
    return ph_now().date()