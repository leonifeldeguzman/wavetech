from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from flask import current_app


class WindyServiceError(Exception):
    """Raised when the Windy Point Forecast API can't be reached or parsed."""


@dataclass
class WindyConditions:
    wind_speed_kmh: float | None
    wind_direction: str | None
    weather_condition: str | None
    temperature_c: float | None
    recorded_at: datetime
    retrieved_at: datetime


_DEGREES_TO_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _degrees_to_compass(deg: float) -> str:
    idx = round(deg / 45) % 8
    return _DEGREES_TO_COMPASS[idx]


def _derive_weather_condition(lclouds: float, mclouds: float, hclouds: float, precip_m: float) -> str:
    """
    Windy's GFS point forecast has no plain-text condition field, so we
    derive one from raw numeric fields into the project's 3 labels:
    Sunny, Cloudy, Rainy.

    Rule (simple and explainable, not a Windy-official formula):
    1. Any meaningful precipitation -> "Rainy" (precip wins over clouds,
       since it's the more operationally relevant fact for ferry ops).
    2. Otherwise, take the highest of the three cloud layers (low/mid/high)
       as "how covered is the sky" and threshold it into Sunny vs Cloudy.
    """
    precip_mm = (precip_m or 0) * 1000  # Windy returns precip in meters
    if precip_mm > 0.1:
        return "Rainy"

    max_cloud_pct = max(lclouds or 0, mclouds or 0, hclouds or 0)
    if max_cloud_pct <= 30:
        return "Sunny"
    return "Cloudy"


def fetch_conditions() -> WindyConditions:
    cfg = current_app.config
    payload = {
        "lat": cfg["WINDY_LAT"],
        "lon": cfg["WINDY_LON"],
        "model": "gfs",
        "parameters": ["wind", "temp", "lclouds", "mclouds", "hclouds", "precip"],
        "levels": ["surface"],
        "key": cfg["WINDY_API_KEY"],
    }

    try:
        response = requests.post(
            cfg["WINDY_API_URL"],
            json=payload,
            timeout=cfg["WINDY_API_TIMEOUT_SECONDS"],
        )
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise WindyServiceError("Windy API request timed out.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise WindyServiceError("Could not connect to Windy API.") from exc
    except requests.exceptions.HTTPError as exc:
        raise WindyServiceError(f"Windy API returned an error: {exc}") from exc

    data = response.json()
    print("WINDY RESPONSE KEYS:", list(data.keys()))   # TEMP — remove after checking


    try:
        wind_u = data["wind_u-surface"][0]
        wind_v = data["wind_v-surface"][0]
        temp_k = data["temp-surface"][0]
    except (KeyError, IndexError) as exc:
        raise WindyServiceError("Unexpected Windy API response shape.") from exc

    # Cloud/precip fields are non-critical: if the key or shape is off,
    # fall back to 0 rather than raising, so a naming mismatch doesn't
    # take down wind/temp (which we know work) along with it.
    lclouds = data.get("lclouds-surface", [0])[0]
    mclouds = data.get("mclouds-surface", [0])[0]
    hclouds = data.get("hclouds-surface", [0])[0]
    precip_m = data.get("precip-surface", [0])[0]

    wind_speed_ms = (wind_u ** 2 + wind_v ** 2) ** 0.5
    wind_speed_kmh = wind_speed_ms * 3.6

    import math
    wind_deg = (math.degrees(math.atan2(-wind_u, -wind_v))) % 360

    return WindyConditions(
        wind_speed_kmh=round(wind_speed_kmh, 1),
        wind_direction=_degrees_to_compass(wind_deg),
        weather_condition=_derive_weather_condition(lclouds, mclouds, hclouds, precip_m),
        temperature_c=round(temp_k - 273.15, 1),
        recorded_at=datetime.now(timezone.utc),
        retrieved_at=datetime.now(timezone.utc),
    )