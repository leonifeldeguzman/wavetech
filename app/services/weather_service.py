"""
Open-Meteo client: per-station weather (wind, temperature, condition).

These are weather-MODEL values for a grid cell of roughly 15 km. They are
a weather proxy, NOT a lake measurement, and stations that fall in the same
grid cell receive identical values (see grid_lat / grid_lon).

Mirrors windy_service.py: one typed exception and a plain dataclass result.
The service does not touch the database; callers pass the stations in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from flask import current_app


class WeatherServiceError(Exception):
    """Raised when Open-Meteo can't be reached or its response can't be used."""


@dataclass
class StationWeather:
    station_no: str
    wind_speed_kmh: float | None
    wind_direction: str | None
    weather_condition: str | None
    temperature_c: float | None
    recorded_at: datetime      # when the model values apply (naive UTC)
    retrieved_at: datetime     # when WaveTech fetched them (naive UTC)
    grid_lat: float | None     # grid cell Open-Meteo actually used
    grid_lon: float | None


_CURRENT_FIELDS = (
    "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code"
)
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# ---------------------------------------------------------------------------
# Weather condition: ONE normalized label per observation.
#
# Open-Meteo returns a WMO weather interpretation code (`weather_code`).
# This table is the single place that turns a code into a WaveTech label;
# Admin and Passenger both read the stored label, so neither side can
# interpret the same observation differently.
#
# Labels are the ones the Safety rules in Admin Settings already know
# ("Sunny", "Cloudy", "Rainy") plus two finer ones ("Partly Cloudy",
# "Thunderstorm"). The safety rule for the finer ones lives in
# scheduling_service._classify_weather(); this module never decides
# Safe/Caution/Unsafe.
# ---------------------------------------------------------------------------
CONDITION_SUNNY = "Sunny"
CONDITION_PARTLY_CLOUDY = "Partly Cloudy"
CONDITION_CLOUDY = "Cloudy"
CONDITION_RAINY = "Rainy"
CONDITION_THUNDERSTORM = "Thunderstorm"

# WMO codes as documented by Open-Meteo (https://open-meteo.com/en/docs).
WMO_CODE_TO_CONDITION = {
    0: CONDITION_SUNNY,            # clear sky
    1: CONDITION_SUNNY,            # mainly clear
    2: CONDITION_PARTLY_CLOUDY,    # partly cloudy
    3: CONDITION_CLOUDY,           # overcast
    45: CONDITION_CLOUDY,          # fog (sky obscured; no separate label)
    48: CONDITION_CLOUDY,          # depositing rime fog
    51: CONDITION_RAINY, 53: CONDITION_RAINY, 55: CONDITION_RAINY,   # drizzle
    56: CONDITION_RAINY, 57: CONDITION_RAINY,                        # freezing drizzle
    61: CONDITION_RAINY, 63: CONDITION_RAINY, 65: CONDITION_RAINY,   # rain
    66: CONDITION_RAINY, 67: CONDITION_RAINY,                        # freezing rain
    80: CONDITION_RAINY, 81: CONDITION_RAINY, 82: CONDITION_RAINY,   # rain showers
    95: CONDITION_THUNDERSTORM,    # thunderstorm
    96: CONDITION_THUNDERSTORM,    # thunderstorm with slight hail
    99: CONDITION_THUNDERSTORM,    # thunderstorm with heavy hail
}


def normalize_weather_code(code) -> str | None:
    """WMO weather code -> WaveTech weather condition label.

    Returns None (shown as "Unavailable") when the code is missing or is
    not in the table. It never guesses a condition.
    """
    if code is None or isinstance(code, bool):
        return None
    try:
        return WMO_CODE_TO_CONDITION.get(int(code))
    except (TypeError, ValueError):
        return None


def _degrees_to_compass(deg: float) -> str:
    return _COMPASS[round(deg / 45) % 8]


def _request(stations: list) -> list[dict]:
    cfg = current_app.config
    params = {
        "latitude": ",".join(str(s.latitude) for s in stations),
        "longitude": ",".join(str(s.longitude) for s in stations),
        "current": _CURRENT_FIELDS,
        "timeformat": "unixtime",
        "timezone": "GMT",
    }
    attempts = max(1, cfg["OPEN_METEO_MAX_ATTEMPTS"])
    last_exc = None
    last_detail = ""
    for _ in range(attempts):
        try:
            response = requests.get(
                cfg["OPEN_METEO_API_URL"],
                params=params,
                timeout=cfg["OPEN_METEO_TIMEOUT_SECONDS"],
            )
            response.raise_for_status()
            data = response.json()
            break
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
            resp = getattr(exc, "response", None)
            if resp is not None:
                last_detail = f" (HTTP {resp.status_code}: {resp.text[:150]})"
    else:
        raise WeatherServiceError(
            f"Open-Meteo request failed after {attempts} attempt(s): "
            f"{type(last_exc).__name__}{last_detail}"
        ) from last_exc

    if isinstance(data, dict):      # a single location comes back as an object
        data = [data]
    if not isinstance(data, list) or len(data) != len(stations):
        raise WeatherServiceError("Open-Meteo returned an unexpected response shape.")
    return data


def fetch_station_weather(stations) -> list[StationWeather]:
    """Fetch current model weather for each station (one HTTP call).

    `stations` is any iterable of objects with station_no, latitude and
    longitude (LakeStation rows). Results are in the same order.
    """
    stations = list(stations)
    if not stations:
        return []

    results = _request(stations)
    retrieved_at = datetime.now(timezone.utc).replace(tzinfo=None)

    out = []
    for station, item in zip(stations, results):
        cur = item.get("current") or {}
        try:
            recorded_at = datetime.fromtimestamp(
                int(cur["time"]), tz=timezone.utc
            ).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherServiceError(
                f"Open-Meteo gave no usable time for station {station.station_no}."
            ) from exc

        speed = cur.get("wind_speed_10m")
        deg = cur.get("wind_direction_10m")
        temp = cur.get("temperature_2m")

        out.append(StationWeather(
            station_no=station.station_no,
            wind_speed_kmh=None if speed is None else round(speed, 1),
            wind_direction=None if deg is None else _degrees_to_compass(deg),
            weather_condition=normalize_weather_code(cur.get("weather_code")),
            temperature_c=None if temp is None else round(temp, 1),
            recorded_at=recorded_at,
            retrieved_at=retrieved_at,
            grid_lat=item.get("latitude"),
            grid_lon=item.get("longitude"),
        ))
    return out