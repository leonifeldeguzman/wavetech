"""UniSMS (unismsapi.com) integration — kept as a parallel, drop-in
alternative to app/services/semaphore_service.py. Not currently used by
any route; nothing in routes.py imports this yet.

Mirrors semaphore_service.py's exact interface (send_sms() / SmsResult)
so that switching which provider is actually used is a one-line import
change in app/blueprints/passenger/routes.py — swap
`from app.services import semaphore_service` for
`from app.services import unisms_service` (and the corresponding
`semaphore_service.send_sms(...)` call), nothing else changes.

Design intent (see project rules — same guarantees as semaphore_service.py):
  - A UniSMS failure must NEVER raise out to the caller and must NEVER
    roll back a database transaction that already succeeded. send_sms()
    always returns a SmsResult; it never raises for ordinary failure
    modes (network error, bad response, missing config).
  - The API key is never logged, printed, or included in any exception
    message.

Reference: https://unismsapi.com/docs/sms (official docs, fetched
2026-09-23). UniSMS authenticates via HTTP Basic Auth using the secret
key as the username with an empty password — NOT an X-API-Key header
like some other providers. Confirmed against their own published
Python sample code.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests
from flask import current_app
from requests.auth import HTTPBasicAuth


@dataclass
class SmsResult:
    success: bool
    status: str  # "sent", "skipped_no_config", "skipped_no_number", "failed"
    error: str | None = None
    raw_response: dict | None = None


def send_sms(number: str | None, message: str) -> SmsResult:
    """Send one SMS via UniSMS. Never raises.

    Returns SmsResult(success=False, status="skipped_no_number") if
    `number` is falsy, and status="skipped_no_config" if
    UNISMS_API_KEY is not set — both are normal, expected outcomes
    (e.g. a passenger who didn't supply a contact number), not errors.

    Per UniSMS's docs, `number` may be in international format
    (+639XXXXXXXXX) or local format (09XXXXXXXXX) — both are accepted
    as-is, no normalization needed here (unlike some other providers).
    """
    if not number:
        return SmsResult(False, "skipped_no_number")

    api_key = current_app.config.get("UNISMS_API_KEY", "")
    if not api_key:
        current_app.logger.warning(
            "UniSMS SMS skipped: UNISMS_API_KEY is not configured."
        )
        return SmsResult(False, "skipped_no_config")

    payload = {
        "recipient": number,
        "content": message,
        "sender_id": current_app.config.get("UNISMS_SENDER_ID", "Unisoft")
    }

    try:
        response = requests.post(
            current_app.config["UNISMS_API_URL"],
            json=payload,
            auth=HTTPBasicAuth(api_key, ""),
            headers={"Content-Type": "application/json"},
            timeout=current_app.config.get("UNISMS_API_TIMEOUT_SECONDS", 10),
        )
    except requests.RequestException as exc:
        # Network-level failure — no response was received at all.
        current_app.logger.warning("UniSMS SMS failed (network): %s", exc)
        return SmsResult(False, "failed", error=str(exc))

    if response.status_code >= 400:
        # Capture the actual response body before it's lost — UniSMS
        # almost certainly explains *why* a 4xx happened in the body,
        # and raise_for_status() alone throws that detail away.
        try:
            error_detail = response.json()
        except ValueError:
            error_detail = response.text
        current_app.logger.warning(
            "UniSMS SMS failed: %s %s", response.status_code, error_detail
        )
        return SmsResult(
            False,
            "failed",
            error=f"{response.status_code}: {error_detail}",
        )

    return SmsResult(True, "sent", raw_response=response.json())    