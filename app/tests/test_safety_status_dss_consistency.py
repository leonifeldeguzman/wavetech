"""The dashboard/passenger Safety Status and the Scheduling Decision-Support
overall condition must come from the same evaluation, so they can never
disagree for the same data.

Run with:
    python -m pytest app/tests/test_safety_status_dss_consistency.py -v
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import monitoring_service, scheduling_service, settings_service

WATER = "app.services.scheduling_service.get_current_water_reading"
WEATHER = "app.services.scheduling_service.monitoring_service.get_dss_weather_conditions"

THRESHOLDS = dict(
    wind_safe_max_kmh=20, wind_caution_max_kmh=30,
    water_safe_min_m=10.50, water_safe_max_m=12.50,
    water_caution_min_m=10.00, water_caution_max_m=13.00,
    weather_safe="Sunny", weather_caution="Cloudy", weather_unsafe="Rainy",
)


def _water(level):
    if level is None:
        return None
    return SimpleNamespace(
        source="manual", water_level_m=level, recorded_at=None,
        retrieved_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _weather(wind=10.0, condition="Sunny", status="live"):
    now = datetime.now(timezone.utc)
    reading = SimpleNamespace(
        wind_speed_kmh=wind, wind_direction="NE", weather_condition=condition,
        temperature_c=28.0, recorded_at=now, retrieved_at=now,
    )
    return {"status": status, "reading": reading, "message": None}


NO_WEATHER = {"status": "unavailable", "reading": None, "message": "x"}

# name, water level, weather dict, expected DSS overall, expected recommendation
CASES = [
    ("safe",               11.5, _weather(),                    "Safe",        "PROCEED"),
    ("caution water",      12.8, _weather(),                    "Caution",     "CAUTION / DELAY"),
    ("caution wind",       11.5, _weather(wind=25),             "Caution",     "CAUTION / DELAY"),
    ("caution weather",    11.5, _weather(condition="Cloudy"),  "Caution",     "CAUTION / DELAY"),
    ("unsafe water",       13.5, _weather(),                    "Unsafe",      "UNSAFE / DO NOT DEPART"),
    ("unsafe wind",        11.5, _weather(wind=45),             "Unsafe",      "UNSAFE / DO NOT DEPART"),
    ("unsafe weather",     11.5, _weather(condition="Rainy"),   "Unsafe",      "UNSAFE / DO NOT DEPART"),
    ("no water reading",   None, _weather(),                    "Unavailable", "UNAVAILABLE"),
    ("no weather",         11.5, NO_WEATHER,                    "Unavailable", "UNAVAILABLE"),
    ("cached weather",     11.5, _weather(status="cached"),     "Unavailable", "UNAVAILABLE"),
    ("unknown weather",    11.5, _weather(condition="Foggy"),   "Unavailable", "UNAVAILABLE"),
    ("missing wind",       11.5, _weather(wind=None),           "Unavailable", "UNAVAILABLE"),
]


@pytest.mark.parametrize("name, level, weather, overall, recommendation",
                         CASES, ids=[c[0] for c in CASES])
def test_dashboard_status_matches_dss(app, name, level, weather, overall, recommendation):
    with app.app_context():
        settings_service.update_safety_thresholds(**THRESHOLDS)
        with patch(WATER, return_value=_water(level)), patch(WEATHER, return_value=weather):
            assessment = scheduling_service.get_assessment()
            status, label, _ = monitoring_service.get_current_safety()
            # Passing the same assessment (as the dashboard route does) agrees too.
            status2, label2, _ = monitoring_service.get_current_safety(assessment=assessment)

        assert assessment.overall_status == overall
        assert assessment.recommendation == recommendation
        assert status == overall.lower() == status2
        assert label == overall.upper() == label2


def test_stale_water_reading_is_unavailable_everywhere(app):
    """A water reading from a previous day cannot support a recommendation,
    so the Safety Status must not show Caution/Safe from it either."""
    with app.app_context():
        from app.extensions import db
        from app.models.environmental_reading import EnvironmentalReading

        settings_service.update_safety_thresholds(**THRESHOLDS)
        db.session.add(EnvironmentalReading(
            source="manual", water_level_m=12.8,
            retrieved_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2),
        ))
        db.session.commit()
        with patch(WEATHER, return_value=_weather()):
            assessment = scheduling_service.get_assessment()
            status, label, _ = monitoring_service.get_current_safety()
        assert assessment.overall_status == "Unavailable"
        assert (status, label) == ("unavailable", "UNAVAILABLE")


def test_changing_thresholds_changes_both_together(app):
    with app.app_context():
        settings_service.update_safety_thresholds(**THRESHOLDS)
        with patch(WATER, return_value=_water(12.8)), patch(WEATHER, return_value=_weather()):
            assert monitoring_service.get_current_safety()[0] == "caution"
            assert scheduling_service.get_assessment().overall_status == "Caution"

            settings_service.update_safety_thresholds(**{**THRESHOLDS, "water_safe_max_m": 13.0})
            assert monitoring_service.get_current_safety()[0] == "safe"
            assert scheduling_service.get_assessment().overall_status == "Safe"


@pytest.mark.parametrize("name, level, weather, overall, recommendation",
                         CASES, ids=[c[0] for c in CASES])
def test_dashboard_page_shows_same_condition_in_both_places(
        app, admin_client, make_boat, make_trip, name, level, weather, overall, recommendation):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")
    with app.app_context():
        settings_service.update_safety_thresholds(**THRESHOLDS)
    with patch(WATER, return_value=_water(level)), patch(WEATHER, return_value=weather):
        html = admin_client.get(f"/dashboard?trip_id={trip_id}").get_data(as_text=True)

    kpi = html.split("Safety Status", 1)[1].split("dashboard-kpi-sub", 1)[0]
    assert overall.upper() in kpi.upper()
    assert recommendation in html
    # Decision Support "Overall Safety" badge shows the same word.
    badge = html.split("Overall Safety", 1)[1].split("Recommendation", 1)[0]
    assert overall.upper() in badge.upper()