"""
LLDA (Laguna Lake Development Authority) environmental data client.

RESEARCH SUMMARY (see docs/llda_integration.md for the full write-up)
----------------------------------------------------------------------
Before writing this module, we looked for an official, machine-readable
LLDA data source (API / JSON / XML / CSV / GIS service) for Laguna de Bay
water-level and lake-condition data. As of this writing:

* LLDA publishes near-real-time water levels from its Radar Level Sensor
  (RLS) network for four stations (South Bay, East Bay, Central Bay, West
  Bay) on a public **HTML** page: https://llda.gov.ph/water-level/ . It is
  updated a few times a day and is a page meant for people to read, not a
  documented API — it returns no JSON/XML, publishes no schema, and the
  site actively blocks automated/bot requests.
* LLDA's own FOI (Freedom of Information) responses confirm there is no
  API: requesters are told to read that same web page, or, for historical
  bulk data, to file a formal FOI request at https://foi.gov.ph (agency:
  "Laguna Lake Development Authority (DENRLLDA)").
* LLDA has stated in an FOI response that it does not monitor river water
  levels at all (that is DOST-ASTI's remit, at philsensors.asti.dost.gov.ph)
  — but Laguna de Bay's LAKE level (what WaveTech's ferry operations care
  about) is LLDA's own RLS network, published only via the HTML page above.

Per the task's constraints, we will NOT scrape that HTML page and call it
an "API" — it is not intended for machine access, and doing so would be
fragile (layout changes silently break it) and against LLDA's evident
intent (bot-blocking). Instead, this module is written against a
*configuration contract* (LLDA_API_URL / LLDA_API_KEY) so that:

1. Today, with no URL configured, it fails safely and predictably
   (LLDANotConfiguredError) and the rest of the system falls back to
   cached/manual data, exactly as the SRS's failure-handling flow requires.
2. If/when WaveTech obtains a real, documented endpoint from LLDA (for
   example through a formal data-sharing agreement, since LLDA does
   entertain research/thesis data requests via FOI), only this module and
   its response parsing need to change — nothing in the dashboard, models,
   or scheduling logic has to move.

Do not point LLDA_API_URL at the public HTML page or at any internal/
undocumented address (e.g. IP-based URLs surfaced in old FOI replies).
Those are not stable, authorized machine interfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from flask import current_app


class LLDAServiceError(Exception):
    """Base class for all LLDA client errors."""


class LLDANotConfiguredError(LLDAServiceError):
    """Raised when no LLDA_API_URL has been configured.

    This is the expected, everyday state until WaveTech is given a real
    LLDA endpoint — see the module docstring. Callers should treat this
    the same as any other "live fetch didn't work" case and fall back to
    cached/manual data.
    """


class LLDATimeoutError(LLDAServiceError):
    """Raised when the LLDA endpoint did not respond within the timeout."""


class LLDAConnectionError(LLDAServiceError):
    """Raised when the LLDA endpoint could not be reached at all."""


class LLDAHTTPError(LLDAServiceError):
    """Raised when LLDA responded with a non-2xx HTTP status."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(message or f"LLDA endpoint returned HTTP {status_code}")


class LLDAParseError(LLDAServiceError):
    """Raised when the LLDA response body could not be parsed into a reading."""


@dataclass
class LLDAReading:
    """A single normalized environmental reading pulled from LLDA."""

    source: str
    station: str
    water_level_m: Optional[float]
    unit: str
    recorded_at: Optional[datetime]
    retrieved_at: datetime
    raw: Any = None


def _get_config(key: str, default=""):
    """Read config from the active Flask app if available, else env-style default."""
    try:
        return current_app.config.get(key, default)
    except RuntimeError:
        # No app context (e.g. called outside a request/app_context) —
        # callers in this codebase always run inside an app context, but
        # fall back gracefully rather than crashing.
        return default


def _parse_payload(payload: dict) -> LLDAReading:
    """Best-effort normalization of an LLDA JSON payload into an LLDAReading.

    NOTE: LLDA does not currently publish a documented response schema
    (see module docstring), so there is no real contract to parse against
    yet. This function accepts a small set of reasonably-named fields and
    is intentionally defensive. Once a real, documented LLDA response
    format exists, update this function (and only this function) to match
    it — the rest of the integration does not need to change.
    """
    if not isinstance(payload, dict):
        raise LLDAParseError("Expected a JSON object in the LLDA response body.")

    water_level = (
        payload.get("water_level_m")
        or payload.get("water_level")
        or payload.get("waterLevel")
    )
    if water_level is None:
        raise LLDAParseError("LLDA response did not include a water level reading.")

    try:
        water_level = float(water_level)
    except (TypeError, ValueError) as exc:
        raise LLDAParseError(f"Water level value was not numeric: {water_level!r}") from exc

    station = (
        payload.get("station")
        or payload.get("station_name")
        or payload.get("location")
        or "Laguna de Bay"
    )

    recorded_at_raw = payload.get("recorded_at") or payload.get("timestamp")
    recorded_at = None
    if recorded_at_raw:
        try:
            recorded_at = datetime.fromisoformat(str(recorded_at_raw).replace("Z", "+00:00"))
        except ValueError:
            recorded_at = None

    return LLDAReading(
        source="llda",
        station=str(station),
        water_level_m=water_level,
        unit=payload.get("unit", "m"),
        recorded_at=recorded_at,
        retrieved_at=datetime.now(timezone.utc),
        raw=payload,
    )


def fetch_water_level() -> LLDAReading:
    """Fetch the current Laguna de Bay water level from LLDA.

    Raises one of the LLDAServiceError subclasses on any failure. Never
    returns partial/None data on failure — callers must catch and fall
    back to cached or manual data instead.
    """
    url = _get_config("LLDA_API_URL", "")
    if not url:
        raise LLDANotConfiguredError(
            "LLDA_API_URL is not configured. No official LLDA API currently "
            "exists (see docs/llda_integration.md); this is expected until "
            "one is obtained."
        )

    api_key = _get_config("LLDA_API_KEY", "")
    timeout = _get_config("LLDA_API_TIMEOUT_SECONDS", 10)

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise LLDATimeoutError(f"LLDA request timed out after {timeout}s") from exc
    except requests.exceptions.ConnectionError as exc:
        raise LLDAConnectionError(f"Could not connect to LLDA endpoint: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise LLDAConnectionError(f"LLDA request failed: {exc}") from exc

    if response.status_code != 200:
        raise LLDAHTTPError(response.status_code)

    try:
        payload = response.json()
    except ValueError as exc:
        raise LLDAParseError("LLDA response body was not valid JSON.") from exc

    return _parse_payload(payload)