"""Rule-based environmental scheduling decision support.

This module is deliberately independent of trip mutation.  It evaluates
current environmental inputs and returns a recommendation for the operator;
it never changes a Trip or passenger manifest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services import llda_mock_service, monitoring_service, settings_service
from app.utils.timezone import to_ph_time

RECOMMENDATION_PROCEED = "PROCEED"
RECOMMENDATION_CAUTION = "CAUTION / DELAY"
RECOMMENDATION_UNSAFE = "UNSAFE"
RECOMMENDATION_UNAVAILABLE = "UNAVAILABLE"

STATUS_SAFE = "Safe"
STATUS_CAUTION = "Caution"
STATUS_UNSAFE = "Unsafe"
STATUS_UNAVAILABLE = "Unavailable"


@dataclass(frozen=True)
class ConditionResult:
    name: str
    value: Any
    status: str
    reason: str


@dataclass(frozen=True)
class SchedulingAssessment:
    recommendation: str
    overall_status: str
    reason: str
    wind_speed: ConditionResult
    water_level: ConditionResult
    weather: ConditionResult
    wind_direction: str | None
    temperature_c: float | None
    windy_retrieved_at: datetime | None
    llda_recorded_at: datetime | None
    last_updated_at: datetime | None
    llda_source_label: str


def _thresholds() -> dict[str, float]:
    """Read the current Safety Thresholds.

    These come from the centralized Admin Settings service (backed by the
    `system_settings` table), NOT directly from app.config, so that a
    threshold saved in Admin Settings takes effect on the very next
    Scheduling Decision-Support evaluation. See app/services/settings_service.py.
    """
    saved = settings_service.get_safety_thresholds()
    return {
        "wind_safe_max": float(saved["wind_safe_max_kmh"]),
        "wind_caution_max": float(saved["wind_caution_max_kmh"]),
        "water_safe_min": float(saved["water_safe_min_m"]),
        "water_safe_max": float(saved["water_safe_max_m"]),
        "water_caution_min": float(saved["water_caution_min_m"]),
        "water_caution_max": float(saved["water_caution_max_m"]),
    }


def _valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _classify_wind(value: Any, thresholds: dict[str, float]) -> ConditionResult:
    if not _valid_number(value) or float(value) < 0:
        return ConditionResult("Wind Speed", value, STATUS_UNAVAILABLE, "Wind speed is missing or invalid.")

    value = float(value)
    if value <= thresholds["wind_safe_max"]:
        return ConditionResult("Wind Speed", value, STATUS_SAFE, "Wind speed is within the temporary safe range.")
    if value <= thresholds["wind_caution_max"]:
        return ConditionResult("Wind Speed", value, STATUS_CAUTION, "Wind speed is within the temporary caution range.")
    return ConditionResult("Wind Speed", value, STATUS_UNSAFE, "Wind speed exceeds the temporary unsafe threshold.")


def _classify_water(value: Any, thresholds: dict[str, float]) -> ConditionResult:
    if not _valid_number(value) or float(value) < 0:
        return ConditionResult("Water Level", value, STATUS_UNAVAILABLE, "Water level is missing or invalid.")

    value = float(value)
    if thresholds["water_safe_min"] <= value <= thresholds["water_safe_max"]:
        return ConditionResult("Water Level", value, STATUS_SAFE, "Water level is within the temporary safe range.")
    if thresholds["water_caution_min"] <= value <= thresholds["water_caution_max"]:
        return ConditionResult("Water Level", value, STATUS_CAUTION, "Water level is within the temporary caution range.")
    return ConditionResult("Water Level", value, STATUS_UNSAFE, "Water level exceeds the temporary unsafe range.")


def _classify_weather(value: Any) -> ConditionResult:
    if not isinstance(value, str) or not value.strip():
        return ConditionResult("Weather", value, STATUS_UNAVAILABLE, "Weather condition is missing.")

    normalized = value.strip().lower()
    categories = settings_service.get_weather_categories()

    if normalized in categories["safe"]:
        return ConditionResult("Weather", value, STATUS_SAFE, "Weather condition is classified as temporarily safe.")
    if normalized in categories["caution"]:
        return ConditionResult("Weather", value, STATUS_CAUTION, "Weather condition is classified as temporarily cautionary.")
    if normalized in categories["unsafe"]:
        return ConditionResult("Weather", value, STATUS_UNSAFE, "Weather condition is classified as temporarily unsafe.")

    return ConditionResult("Weather", value, STATUS_UNAVAILABLE, "Weather condition is not recognized by the decision rules.")


def evaluate_conditions(windy_reading: Any, llda_reading: Any) -> SchedulingAssessment:
    """Evaluate required environmental readings without mutating any DB row."""
    thresholds = _thresholds()

    if windy_reading is None:
        wind = ConditionResult("Wind Speed", None, STATUS_UNAVAILABLE, "Windy data is unavailable.")
        weather = ConditionResult("Weather", None, STATUS_UNAVAILABLE, "Windy weather data is unavailable.")
        direction = None
        temperature = None
        windy_retrieved = None
    else:
        wind = _classify_wind(getattr(windy_reading, "wind_speed_kmh", None), thresholds)
        weather = _classify_weather(getattr(windy_reading, "weather_condition", None))
        direction = getattr(windy_reading, "wind_direction", None)
        temperature = getattr(windy_reading, "temperature_c", None)
        windy_retrieved = getattr(windy_reading, "retrieved_at", None)

    if llda_reading is None:
        water = ConditionResult("Water Level", None, STATUS_UNAVAILABLE, "Water level data is unavailable.")
        llda_recorded = None
    else:
        water = _classify_water(getattr(llda_reading, "water_level_m", None), thresholds)
        llda_recorded = getattr(llda_reading, "recorded_at", None)

    results = [wind, water, weather]
    if any(r.status == STATUS_UNAVAILABLE for r in results):
        recommendation = RECOMMENDATION_UNAVAILABLE
        overall = STATUS_UNAVAILABLE
        reason = "Required environmental data is missing or invalid. Do not use this assessment to approve a departure."
    elif any(r.status == STATUS_UNSAFE for r in results):
        recommendation = RECOMMENDATION_UNSAFE
        overall = STATUS_UNSAFE
        unsafe = ", ".join(r.name for r in results if r.status == STATUS_UNSAFE)
        reason = f"Unsafe condition detected: {unsafe}. Operator review is required."
    elif any(r.status == STATUS_CAUTION for r in results):
        recommendation = RECOMMENDATION_CAUTION
        overall = STATUS_CAUTION
        caution = ", ".join(r.name for r in results if r.status == STATUS_CAUTION)
        reason = f"Caution condition detected: {caution}. Consider delaying after operator review."
    else:
        recommendation = RECOMMENDATION_PROCEED
        overall = STATUS_SAFE
        reason = "All required environmental conditions are within the temporary system-defined safe ranges."

    timestamps = []
    if windy_retrieved is not None:
        timestamps.append(windy_retrieved)

    if llda_recorded is not None:
      timestamps.append(llda_recorded)

    normalized_timestamps = [
        timestamp.replace(tzinfo=None)
        for timestamp in timestamps
    ]

    last_updated_at = (
        max(normalized_timestamps)
        if normalized_timestamps
        else None
    )

    return SchedulingAssessment(
        recommendation=recommendation,
        overall_status=overall,
        reason=reason,
        wind_speed=wind,
        water_level=water,
        weather=weather,
        wind_direction=direction,
        temperature_c=temperature,
        windy_retrieved_at=windy_retrieved,
        llda_recorded_at=llda_recorded,
        last_updated_at=last_updated_at,
        llda_source_label="MOCK/DEVELOPMENT DATA",
    )


def get_assessment() -> SchedulingAssessment:
    """Assess the latest/current environmental conditions.

    The assessment uses the latest available monitoring readings and does not
    use a trip's future departure time or forecast data. The assessment is
    recommendation-only and never changes a Trip or passenger manifest.
    The LLDA value remains the temporary development mock until an authorized
    LLDA machine-readable source is available.
    """
    windy = monitoring_service.get_windy_conditions()
    windy_reading = (
        windy.get("reading")
        if windy.get("status") == monitoring_service.STATUS_LIVE
        else None
    )

    try:
        llda_reading = llda_mock_service.fetch_water_level()
    except (TypeError, ValueError):
        llda_reading = None

    return evaluate_conditions(windy_reading, llda_reading)

def format_timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return to_ph_time(dt).strftime("%B %d, %Y %I:%M %p PHT")
