"""
Philippine Time (Asia/Manila, UTC+8) display helpers.

WaveTech stores timestamps as naive `DateTime` columns (see
EnvironmentalReading.retrieved_at / recorded_at), populated either by
`db.func.now()` (Postgres server clock) or by `datetime.now(timezone.utc)`
in the LLDA/Windy services with the tzinfo stripped before storage — see
`to_naive_utc` below. In both cases the stored value is UTC wall-clock
time with no tzinfo attached.

We deliberately do NOT change the database/column timezone or store
localized timestamps — that would be a much bigger, riskier change and
would break comparisons/ordering that already rely on UTC (e.g.
`order_by(retrieved_at.desc())`). Instead, conversion to Philippine Time
happens ONLY at display time, via the `ph_time` Jinja filter registered
in app/__init__.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PH_TZ = ZoneInfo("Asia/Manila")


def to_naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to naive UTC before it goes into a DateTime column.

    Accepts naive datetimes (assumed already UTC) and tz-aware datetimes
    (converted to UTC, then stripped of tzinfo). Used when storing
    recorded_at from LLDA/Windy, which are produced as
    `datetime.now(timezone.utc)` in those services.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=None)
    return dt


def to_ph_time(dt: datetime | None) -> datetime | None:
    """Convert a stored (naive, assumed-UTC) datetime to Asia/Manila for display."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PH_TZ)


def format_ph_time(dt: datetime | None, fmt: str = "%B %d, %Y %I:%M %p") -> str:
    """Jinja filter: format a stored UTC timestamp as Philippine Time text."""
    ph_dt = to_ph_time(dt)
    if ph_dt is None:
        return "—"
    return f"{ph_dt.strftime(fmt)} PHT"