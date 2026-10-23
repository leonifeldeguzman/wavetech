"""Water-level Safety Status (Admin dashboard + Passenger home) must follow
the Safety Thresholds saved in Admin Settings -- no hardcoded limits.

get_current_safety() is the overall Decision-Support condition (water, wind
and weather; see test_safety_status_dss_consistency.py). These tests isolate
the WATER part by giving it a live, safe weather reading, so the water level
is the only thing that can change the result."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service, scheduling_service, settings_service


@pytest.fixture(autouse=True)
def _safe_live_weather():
    now = datetime.now(timezone.utc)
    reading = SimpleNamespace(
        wind_speed_kmh=10.0, wind_direction="NE", weather_condition="Sunny",
        temperature_c=28.0, recorded_at=now, retrieved_at=now,
    )
    with patch(
        "app.services.scheduling_service.monitoring_service.get_dss_weather_conditions",
        return_value={"status": "live", "reading": reading, "message": None},
    ):
        yield


def _save(**water):
    t = settings_service.get_safety_thresholds()
    t.update(water)
    settings_service.update_safety_thresholds(**t)


def _add_reading(level):
    db.session.add(EnvironmentalReading(source="manual", water_level_m=level))
    db.session.commit()


@pytest.mark.parametrize("level, expected", [
    (10.50, "safe"), (11.5, "safe"), (12.50, "safe"),
    (10.00, "caution"), (10.20, "caution"), (12.51, "caution"),
    (12.8, "caution"), (12.99, "caution"),
    # Below the low threshold is Low Water Level (Delay), not Unsafe; the
    # configured critical-high limit (water_caution_max_m) is where Unsafe starts.
    (9.99, "caution"), (13.00, "unsafe"), (13.01, "unsafe"),
])
def test_classification_follows_saved_settings(app, level, expected):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        _add_reading(level)
        status, label, _ = monitoring_service.get_current_safety()
        assert status == expected
        assert label == expected.upper()


def test_changing_settings_changes_status_without_code_change(app):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        _add_reading(12.8)
        assert monitoring_service.get_current_safety()[0] == "caution"

        # Admin widens the safe range -> same reading is now Safe.
        _save(water_safe_max_m=13.00)
        assert monitoring_service.get_current_safety()[0] == "safe"

        # Admin tightens the caution range -> same reading is now Unsafe.
        _save(water_safe_max_m=12.50, water_caution_max_m=12.60)
        assert monitoring_service.get_current_safety()[0] == "unsafe"


def test_no_reading_is_unavailable(app):
    # No water level recorded: never guessed as safe or caution.
    with app.app_context():
        status, label, reading = monitoring_service.get_current_safety()
        assert (status, label, reading) == ("unavailable", "UNAVAILABLE", None)


def test_matches_decision_support_water_classification(app):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        for level in (9.9, 10.0, 11.0, 12.8, 13.0, 13.2):
            dss = scheduling_service._classify_water(
                level, scheduling_service._thresholds()).status.lower()
            assert monitoring_service.classify_water_safety(level)[0] == dss


def test_overall_condition_uses_new_statuses(app):
    safe_env = {"status": scheduling_service.STATUS_SAFE}
    assert scheduling_service.get_overall_condition("unsafe", safe_env)["status"] == scheduling_service.STATUS_UNSAFE
    assert scheduling_service.get_overall_condition("caution", safe_env)["status"] == scheduling_service.STATUS_CAUTION
    assert scheduling_service.get_overall_condition("safe", safe_env)["status"] == scheduling_service.STATUS_SAFE


# ---------------------------------------------------------------------------
# Water-level status + recommendation. The limits below are SAVED through the
# Admin Settings service in each test (never read from constants in app code):
#   water_safe_min_m    = low threshold
#   water_safe_max_m    = normal upper limit
#   water_caution_max_m = critical-high limit
# The system status is the CONDITION ONLY; the recommendation is the existing
# combined one (no water-specific recommendation exists).
# ---------------------------------------------------------------------------

SAFE, LOW, HIGH, CRITICAL = "SAFE", "LOW WATER LEVEL", "HIGH WATER LEVEL", "CRITICAL HIGH"
# status label -> (existing severity tier, existing combined DSS recommendation)
EXPECTED = {
    SAFE: ("Safe", scheduling_service.RECOMMENDATION_PROCEED),
    LOW: ("Caution", scheduling_service.RECOMMENDATION_CAUTION),
    HIGH: ("Caution", scheduling_service.RECOMMENDATION_CAUTION),
    CRITICAL: ("Unsafe", scheduling_service.RECOMMENDATION_UNSAFE),
}


def _dss(level):
    t = scheduling_service._thresholds()
    water = scheduling_service._classify_water(level, t)
    wind = SimpleNamespace(wind_speed_kmh=10.0, weather_condition="Sunny",
                           wind_direction="NE", temperature_c=28.0,
                           retrieved_at=None)
    llda = SimpleNamespace(water_level_m=level, recorded_at=None, retrieved_at=None,
                           source="manual")
    return water, scheduling_service.evaluate_conditions(wind, llda)


def _reference_config():
    _save(water_caution_min_m=10.00, water_safe_min_m=10.82,
          water_safe_max_m=11.20, water_caution_max_m=12.50)


@pytest.mark.parametrize("level, label", [
    (10.81, LOW), (10.82, SAFE), (11.20, SAFE), (11.21, HIGH),
    (12.49, HIGH), (12.50, CRITICAL), (12.51, CRITICAL),
])
def test_water_status_and_recommendation_at_boundaries(app, level, label):
    with app.app_context():
        _reference_config()
        water, assessment = _dss(level)
        tier, overall_rec = EXPECTED[label]
        assert water.label == label                       # condition only
        assert water.status == tier
        assert assessment.recommendation == overall_rec   # existing combined recommendation
        # Monitoring card uses the same classifier.
        assert monitoring_service.classify_water_safety(level) == (tier.lower(), label)


def test_status_text_never_contains_the_recommendation(app):
    with app.app_context():
        _reference_config()
        for level in (10.81, 10.82, 11.21, 12.50):
            water, _ = _dss(level)
            assert water.label in (SAFE, LOW, HIGH, CRITICAL)
            for word in ("Delay", "Suspend", "Proceed", "Do not depart", "/", "Caution"):
                assert word.lower() not in water.label.lower()


def test_changing_all_three_admin_thresholds_moves_the_boundaries(app):
    with app.app_context():
        _reference_config()
        assert _dss(10.75)[0].label == LOW
        assert _dss(11.25)[0].label == HIGH
        assert _dss(12.55)[0].label == CRITICAL

        _save(water_safe_min_m=10.70, water_safe_max_m=11.30, water_caution_max_m=12.60)
        for level, label in [
            (10.69, LOW), (10.70, SAFE), (10.75, SAFE), (11.30, SAFE),
            (11.31, HIGH), (12.59, HIGH), (12.60, CRITICAL), (12.61, CRITICAL),
        ]:
            water, assessment = _dss(level)
            assert water.label == label, level
            assert assessment.recommendation == EXPECTED[label][1], level


def test_missing_or_invalid_water_level_is_still_unavailable(app):
    with app.app_context():
        for bad in (None, "abc", float("nan"), -1):
            water = scheduling_service._classify_water(bad, scheduling_service._thresholds())
            assert water.status == scheduling_service.STATUS_UNAVAILABLE
            assert water.label is None