"""Semaphore SMS integration — the single place that talks to the
Semaphore API, mirroring how llda_service.py / windy_service.py wrap
their respective external APIs.

Callers (registration flow, announcement publish flow) should never
import `requests` or build Semaphore payloads directly — always go
through send_sms() here, so there is exactly one place that knows the
Semaphore request/response shape and exactly one place the API key is
read from.

Design intent (see project rules):
  - A Semaphore failure must NEVER raise out to the caller and must
    NEVER roll back a database transaction that already succeeded.
    send_sms() always returns a SmsResult; it never raises for ordinary
    failure modes (network error, bad response, missing config).
  - The API key is never logged, printed, or included in any exception
    message.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests
from flask import current_app


@dataclass
class SmsResult:
    success: bool
    status: str  # "sent", "skipped_no_config", "skipped_no_number", "failed"
    error: str | None = None
    raw_response: dict | None = None


def send_sms(number: str | None, message: str) -> SmsResult:
    """Send one SMS via Semaphore. Never raises.

    Returns SmsResult(success=False, status="skipped_no_number") if
    `number` is falsy, and status="skipped_no_config" if
    SEMAPHORE_API_KEY is not set — both are normal, expected outcomes
    (e.g. a passenger who didn't supply a contact number), not errors.
    """
    if not number:
        return SmsResult(success=False, status="skipped_no_number")

    api_key = current_app.config.get("SEMAPHORE_API_KEY", "")
    if not api_key:
        current_app.logger.warning(
            "Semaphore SMS skipped: SEMAPHORE_API_KEY is not configured."
        )
        return SmsResult(success=False, status="skipped_no_config")

    payload = {
        "apikey": api_key,
        "number": number,
        "message": message,
    }
    sender_name = current_app.config.get("SEMAPHORE_SENDER_NAME", "")
    if sender_name:
        payload["sendername"] = sender_name

    try:
        response = requests.post(
            current_app.config["SEMAPHORE_API_URL"],
            data=payload,
            timeout=current_app.config.get("SEMAPHORE_API_TIMEOUT_SECONDS", 10),
        )
        response.raise_for_status()
        return SmsResult(success=True, status="sent", raw_response=response.json())
    except requests.RequestException as exc:
        # Never include payload (contains api key) in the logged error.
        current_app.logger.warning("Semaphore SMS failed: %s", exc)
        return SmsResult(success=False, status="failed", error=str(exc))