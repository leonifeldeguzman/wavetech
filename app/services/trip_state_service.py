"""Single source of truth for whether a trip is a closed (read-only) record.

A trip is read-only when it has Departed, or when its scheduled departure
(date AND time, Asia/Manila) has already passed while it is still in a live
status (the same rule as the grey "Expired" badge on /manifests). A future
trip is never read-only just because its clock time is earlier than now:
the comparison is between full datetimes, not times of day.

Used by the manifest routes, the Dashboard DSS routes and the templates so
the UI and the backend can never disagree.
"""
from __future__ import annotations

from app.utils.ph_clock import ph_now

LIVE_STATUSES = ("Open", "Boarding", "Full", "Delayed")

DEPARTED_MESSAGE = "This trip has departed. Records are read-only."
EXPIRED_MESSAGE = "This trip has expired. Records are read-only."


def is_departed(trip) -> bool:
    return trip.status == "Departed"


def is_expired(trip, now=None) -> bool:
    """True when a live-status trip's departure datetime (PH) has passed.

    ``now`` is the current PH time. Route modules pass their own
    ``ph_now()`` so the project's tests, which patch ``ph_now`` on those
    modules, control the clock; it defaults to the real PH clock.
    """
    if now is None:
        now = ph_now()
    return trip.status in LIVE_STATUSES and trip.departure_time < now


def is_read_only(trip, now=None) -> bool:
    return is_departed(trip) or is_expired(trip, now)


def read_only_message(trip, now=None) -> str | None:
    """Banner text, or None when the trip is still editable."""
    if is_departed(trip):
        return DEPARTED_MESSAGE
    if is_expired(trip, now):
        return EXPIRED_MESSAGE
    return None


def summary_details_url(trip) -> str | None:
    """Analytics > Manifest Records > View link for this trip, or None.

    Analytics opens a record's details with /analytics?view=<trip_id>. The
    link is only offered when analytics_service.get_manifest_detail() finds
    a record for the trip, so a trip without one never gets a broken link.
    """
    from flask import url_for

    from app.services import analytics_service

    if analytics_service.get_manifest_detail(trip.id) is None:
        return None
    return url_for("analytics.index", view=trip.id)