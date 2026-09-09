"""Temporary development-only LLDA water-level provider for scheduling.

This provider is intentionally isolated from the real LLDA client.  It does
not claim to represent official LLDA data and is designed to be replaced by
``llda_service.fetch_water_level`` when an authorized, documented LLDA
machine-readable source becomes available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from flask import current_app


@dataclass(frozen=True)
class MockLLDAReading:
    source: str
    station: str
    water_level_m: float
    recorded_at: datetime


def fetch_water_level() -> MockLLDAReading:
    """Return one clearly-labelled sample water-level reading.

    The timestamp is generated at request time so the dashboard can show when
    the development sample was produced.  The value itself is configurable so
    developers can exercise different scheduling outcomes without changing
    scheduling logic.
    """
    water_level = float(current_app.config.get("LLDA_MOCK_WATER_LEVEL_M", 12.10))
    station = current_app.config.get(
        "LLDA_MOCK_STATION", "Central Bay, Cardona, Rizal"
    )

    if water_level < 0:
        raise ValueError("LLDA mock water level cannot be negative.")

    return MockLLDAReading(
        source="llda_mock",
        station=station,
        water_level_m=water_level,
        recorded_at=datetime.now(timezone.utc),
    )
