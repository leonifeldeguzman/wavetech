"""Centralized Admin Settings service.

This is the single place Safety Thresholds, the Refresh Interval, and the
Session Timeout are read from and written to. It exists so that:

    Admin Settings -> Safety Threshold Configuration -> Scheduling
    Decision-Support -> Safety Evaluation -> PROCEED / CAUTION / UNSAFE

never has threshold values duplicated across files. Scheduling
Decision-Support (app/services/scheduling_service.py) and the
environmental auto-refresh (app/services/monitoring_service.py,
dashboard/monitoring routes) both call into this module rather than
reading app.config directly, so a change saved in Admin Settings takes
effect immediately for both.

Until an Admin saves settings for the first time, the existing
environment-variable-backed `app.config` values (see app/config.py) are
used as defaults for the singleton row.
"""
from __future__ import annotations

from flask import current_app

from app.extensions import db
from app.models.system_setting import SystemSetting

SETTINGS_ROW_ID = 1

# Selectable auto-refresh durations shown in Admin Settings.
REFRESH_INTERVAL_OPTIONS = [10, 30, 60, 300, 600]

MIN_SESSION_TIMEOUT_MINUTES = 1
MAX_SESSION_TIMEOUT_MINUTES = 1440  # 24 hours


class SettingsValidationError(ValueError):
    """Raised when an Admin Settings update fails validation."""


def get_settings() -> SystemSetting:
    """Return the singleton settings row, creating it from app.config defaults
    the first time it's needed."""
    settings = db.session.get(SystemSetting, SETTINGS_ROW_ID)
    if settings is not None:
        return settings

    settings = SystemSetting(
        id=SETTINGS_ROW_ID,
        wind_safe_max_kmh=float(current_app.config["SCHEDULING_WIND_SAFE_MAX_KMH"]),
        wind_caution_max_kmh=float(current_app.config["SCHEDULING_WIND_CAUTION_MAX_KMH"]),
        water_safe_min_m=float(current_app.config["SCHEDULING_WATER_SAFE_MIN_M"]),
        water_safe_max_m=float(current_app.config["SCHEDULING_WATER_SAFE_MAX_M"]),
        water_caution_min_m=float(current_app.config["SCHEDULING_WATER_CAUTION_MIN_M"]),
        water_caution_max_m=float(current_app.config["SCHEDULING_WATER_CAUTION_MAX_M"]),
        weather_safe=str(current_app.config["SCHEDULING_WEATHER_SAFE"]),
        weather_caution=str(current_app.config["SCHEDULING_WEATHER_CAUTION"]),
        weather_unsafe=str(current_app.config["SCHEDULING_WEATHER_UNSAFE"]),
        refresh_interval_seconds=int(current_app.config["REFRESH_INTERVAL_SECONDS"]),
        session_timeout_minutes=30,
    )
    db.session.add(settings)
    db.session.commit()
    return settings


# ---------------------------------------------------------------------------
# Safety Thresholds
# ---------------------------------------------------------------------------

def get_safety_thresholds() -> dict:
    settings = get_settings()
    return {
        "wind_safe_max_kmh": settings.wind_safe_max_kmh,
        "wind_caution_max_kmh": settings.wind_caution_max_kmh,
        "water_safe_min_m": settings.water_safe_min_m,
        "water_safe_max_m": settings.water_safe_max_m,
        "water_caution_min_m": settings.water_caution_min_m,
        "water_caution_max_m": settings.water_caution_max_m,
        "weather_safe": settings.weather_safe,
        "weather_caution": settings.weather_caution,
        "weather_unsafe": settings.weather_unsafe,
    }


def get_weather_categories() -> dict:
    """Return each weather category as a set of normalized (lowercased,
    trimmed) condition labels, splitting on commas so an Admin can list
    more than one condition per category (e.g. "Sunny, Partly Cloudy")."""
    settings = get_settings()

    def _split(raw: str) -> set[str]:
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    return {
        "safe": _split(settings.weather_safe),
        "caution": _split(settings.weather_caution),
        "unsafe": _split(settings.weather_unsafe),
    }


def _parse_required_float(raw, field_label: str) -> float:
    if raw is None or str(raw).strip() == "":
        raise SettingsValidationError(f"{field_label} is required.")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsValidationError(f"{field_label} must be a number.")
    return value


def _parse_weather_field(raw, field_label: str) -> str:
    if raw is None or str(raw).strip() == "":
        raise SettingsValidationError(f"{field_label} is required.")
    return str(raw).strip()


def update_safety_thresholds(
    *,
    wind_safe_max_kmh,
    wind_caution_max_kmh,
    water_safe_min_m,
    water_safe_max_m,
    water_caution_min_m,
    water_caution_max_m,
    weather_safe,
    weather_caution,
    weather_unsafe,
    updated_by_user_id=None,
) -> dict:
    """Validate and persist new Safety Threshold values.

    Raises SettingsValidationError on any invalid/conflicting input,
    leaving the previously saved thresholds untouched. Returns a dict
    describing what changed, suitable for the Activity Log.
    """
    wind_safe_max = _parse_required_float(wind_safe_max_kmh, "Wind Speed safe limit")
    wind_caution_max = _parse_required_float(wind_caution_max_kmh, "Wind Speed caution limit")

    water_safe_min = _parse_required_float(water_safe_min_m, "Water Level safe range (min)")
    water_safe_max = _parse_required_float(water_safe_max_m, "Water Level safe range (max)")
    water_caution_min = _parse_required_float(water_caution_min_m, "Water Level caution range (min)")
    water_caution_max = _parse_required_float(water_caution_max_m, "Water Level caution range (max)")

    weather_safe = _parse_weather_field(weather_safe, "Weather safe conditions")
    weather_caution = _parse_weather_field(weather_caution, "Weather caution conditions")
    weather_unsafe = _parse_weather_field(weather_unsafe, "Weather unsafe conditions")

    # --- Wind Speed validation ---
    if wind_safe_max < 0 or wind_caution_max < 0:
        raise SettingsValidationError("Wind speed limits cannot be negative.")
    if wind_caution_max <= wind_safe_max:
        raise SettingsValidationError(
            "Wind Speed caution limit must be greater than the safe limit."
        )

    # --- Water Level validation ---
    for label, value in (
        ("Water Level safe range (min)", water_safe_min),
        ("Water Level safe range (max)", water_safe_max),
        ("Water Level caution range (min)", water_caution_min),
        ("Water Level caution range (max)", water_caution_max),
    ):
        if value < 0:
            raise SettingsValidationError(f"{label} cannot be negative.")

    if water_safe_min >= water_safe_max:
        raise SettingsValidationError(
            "Water Level safe range minimum must be less than the maximum."
        )
    if water_caution_min > water_safe_min or water_caution_max < water_safe_max:
        raise SettingsValidationError(
            "Water Level caution range must fully surround the safe range."
        )
    if water_caution_min >= water_caution_max:
        raise SettingsValidationError(
            "Water Level caution range minimum must be less than the maximum."
        )

    # --- Weather Conditions validation ---
    def _split(raw: str) -> set[str]:
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    safe_set = _split(weather_safe)
    caution_set = _split(weather_caution)
    unsafe_set = _split(weather_unsafe)

    if not (safe_set and caution_set and unsafe_set):
        raise SettingsValidationError("Weather safe/caution/unsafe conditions cannot be empty.")

    if (safe_set & caution_set) or (safe_set & unsafe_set) or (caution_set & unsafe_set):
        raise SettingsValidationError(
            "A weather condition cannot appear in more than one category "
            "(safe/caution/unsafe)."
        )

    settings = get_settings()

    before = get_safety_thresholds()

    settings.wind_safe_max_kmh = wind_safe_max
    settings.wind_caution_max_kmh = wind_caution_max
    settings.water_safe_min_m = water_safe_min
    settings.water_safe_max_m = water_safe_max
    settings.water_caution_min_m = water_caution_min
    settings.water_caution_max_m = water_caution_max
    settings.weather_safe = weather_safe
    settings.weather_caution = weather_caution
    settings.weather_unsafe = weather_unsafe
    settings.updated_by_user_id = updated_by_user_id

    db.session.commit()

    after = get_safety_thresholds()
    changed = {
        key: {"from": before[key], "to": after[key]}
        for key in before
        if before[key] != after[key]
    }
    return changed


# ---------------------------------------------------------------------------
# Refresh Interval
# ---------------------------------------------------------------------------

def get_refresh_interval_seconds() -> int:
    return get_settings().refresh_interval_seconds


def update_refresh_interval(raw_seconds) -> int:
    try:
        seconds = int(raw_seconds)
    except (TypeError, ValueError):
        raise SettingsValidationError("Refresh interval must be a whole number of seconds.")

    if seconds not in REFRESH_INTERVAL_OPTIONS:
        raise SettingsValidationError(
            "Refresh interval must be one of: "
            + ", ".join(str(s) for s in REFRESH_INTERVAL_OPTIONS) + " seconds."
        )

    settings = get_settings()
    settings.refresh_interval_seconds = seconds
    db.session.commit()
    return seconds


# ---------------------------------------------------------------------------
# Session Timeout
# ---------------------------------------------------------------------------

def get_session_timeout_minutes() -> int:
    return get_settings().session_timeout_minutes


def update_session_timeout(raw_minutes) -> int:
    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        raise SettingsValidationError("Session timeout must be a whole number of minutes.")

    if not (MIN_SESSION_TIMEOUT_MINUTES <= minutes <= MAX_SESSION_TIMEOUT_MINUTES):
        raise SettingsValidationError(
            f"Session timeout must be between {MIN_SESSION_TIMEOUT_MINUTES} and "
            f"{MAX_SESSION_TIMEOUT_MINUTES} minutes."
        )

    settings = get_settings()
    settings.session_timeout_minutes = minutes
    db.session.commit()
    return minutes
