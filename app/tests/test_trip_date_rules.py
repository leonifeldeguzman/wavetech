"""Tests for the Oct 23 date rules: Active Trips, Expired badge, Departed Boats.

A trip's status alone never makes it 'active' or 'departed today': its
departure_time must also fall inside today's PH day.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, time
from unittest.mock import patch

from flask import template_rendered

from app.services import monitoring_service
from app.utils.ph_clock import ph_today

# Same patch targets as test_operator_observation.py, so /dashboard never
# depends on the local database.
_WEATHER = "app.services.scheduling_service.monitoring_service.get_dss_weather_conditions"
_WATER = "app.services.scheduling_service.get_current_water_reading"
_OBS = "app.services.scheduling_service.monitoring_service.get_todays_observation"

_NO_WEATHER = {
    "status": monitoring_service.STATUS_UNAVAILABLE,
    "reading": None,
    "message": "Test: no weather data",
}


def _today_noon():
    return datetime.combine(ph_today(), time(12, 0))


def _days_ago(n):
    return _today_noon() - timedelta(days=n)


@contextmanager
def _captured_context(app):
    """Collect (template name, context) for every template rendered."""
    recorded = []

    def record(sender, template, context, **extra):
        recorded.append((template.name, context))

    template_rendered.connect(record, app)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(record, app)


def _dashboard_context(app, client):
    """Load /dashboard with the three patches; return index.html's context."""
    with patch(_WEATHER, return_value=_NO_WEATHER), \
            patch(_WATER, return_value=None), \
            patch(_OBS, return_value=None):
        with _captured_context(app) as recorded:
            response = client.get("/dashboard")
    assert response.status_code == 200
    for name, context in recorded:
        if name == "dashboard/index.html":
            return context
    raise AssertionError("dashboard/index.html was not rendered")


# ---------------------------------------------------------------- Active Trips

def test_active_trips_counts_today_and_ignores_old_open_trip(
        app, admin_client, make_boat, make_trip):
    boat_id = make_boat()
    make_trip(boat_id=boat_id, status="Open", departure_time=_today_noon())
    make_trip(boat_id=boat_id, status="Open", departure_time=_days_ago(3))

    context = _dashboard_context(app, admin_client)

    assert context["active_trips_count"] == 1


def test_active_trips_status_list_includes_full_and_excludes_finished(
        app, admin_client, make_boat, make_trip):
    boat_id = make_boat()
    for status in ("Open", "Boarding", "Full", "Delayed"):
        make_trip(boat_id=boat_id, status=status, departure_time=_today_noon())
    # Today, but not active:
    make_trip(boat_id=boat_id, status="Cancelled", departure_time=_today_noon())
    make_trip(boat_id=boat_id, status="Departed", departure_time=_today_noon())

    context = _dashboard_context(app, admin_client)

    assert context["active_trips_count"] == 4

# -------------------------------------------------------------- Departed Boats

def test_departed_boats_counts_only_trips_departed_today(
        app, admin_client, make_boat, make_trip):
    boat_id = make_boat()
    make_trip(boat_id=boat_id, status="Departed", departure_time=_today_noon())
    # Old departed trip: must not count.
    make_trip(boat_id=boat_id, status="Departed", departure_time=_days_ago(2))
    # Today but not departed: must not count.
    make_trip(boat_id=boat_id, status="Open", departure_time=_today_noon())

    context = _dashboard_context(app, admin_client)

    assert context["departed_trips_count"] == 1


# --------------------------------------------------------------- Expired badge

def _manifests_page(app, client, day):
    """Load /manifests for a date with the three patches; return (html, expired_ids)."""
    with patch(_WEATHER, return_value=_NO_WEATHER), \
            patch(_WATER, return_value=None), \
            patch(_OBS, return_value=None):
        with _captured_context(app) as recorded:
            response = client.get(f"/manifests?date={day.isoformat()}")
    assert response.status_code == 200
    for name, context in recorded:
        if name == "manifests/list.html":
            return response.get_data(as_text=True), context["expired_ids"]
    raise AssertionError("manifests/list.html was not rendered")


def test_expired_badge_shows_for_past_open_trip(
        app, admin_client, make_boat, make_trip):
    boat_id = make_boat()
    past_id = make_trip(boat_id=boat_id, status="Open",
                        departure_time=_days_ago(3))

    html, expired_ids = _manifests_page(
        app, admin_client, (_days_ago(3)).date())

    assert past_id in expired_ids
    assert "status-expired" in html


def test_expired_badge_not_shown_for_today_or_finished_trips(
        app, admin_client, make_boat, make_trip):
    boat_id = make_boat()
    today_id = make_trip(boat_id=boat_id, status="Open",
                         departure_time=_today_noon())
    departed_id = make_trip(boat_id=boat_id, status="Departed",
                            departure_time=_days_ago(3))
    cancelled_id = make_trip(boat_id=boat_id, status="Cancelled",
                             departure_time=_days_ago(3))

    # Today's page: an Open trip later today is not expired.
    html_today, expired_today = _manifests_page(app, admin_client, ph_today())
    assert today_id not in expired_today
    assert "status-expired" not in html_today

    # A past day: Departed and Cancelled trips are finished, not expired.
    html_past, expired_past = _manifests_page(
        app, admin_client, _days_ago(3).date())
    assert departed_id not in expired_past
    assert cancelled_id not in expired_past
    assert "status-expired" not in html_past