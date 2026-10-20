"""Operator observation: Rough water can raise a result to Caution, never
lowers one, and only counts if recorded today (PH day). No external requests.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service, scheduling_service as ss


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cond(name, value, status):
    return ss.ConditionResult(name, value, status, "test")


_RECOMMENDATION = {
    ss.STATUS_SAFE: ss.RECOMMENDATION_PROCEED,
    ss.STATUS_CAUTION: ss.RECOMMENDATION_CAUTION,
    ss.STATUS_UNSAFE: ss.RECOMMENDATION_UNSAFE,
    ss.STATUS_UNAVAILABLE: ss.RECOMMENDATION_UNAVAILABLE,
}


def _assessment(overall, wind=ss.STATUS_SAFE, water=ss.STATUS_SAFE,
                weather=ss.STATUS_SAFE):
    return ss.SchedulingAssessment(
        recommendation=_RECOMMENDATION[overall],
        overall_status=overall,
        reason="Base reason.",
        wind_speed=_cond("Wind Speed", 5.0, wind),
        water_level=_cond("Water Level", 11.5, water),
        weather=_cond("Weather", "Sunny", weather),
        wind_direction="E",
        temperature_c=30.0,
        windy_retrieved_at=None,
        llda_recorded_at=None,
        last_updated_at=None,
        llda_source_label="Manual observation",
    )


def _obs(condition, comment=None):
    return SimpleNamespace(observed_condition=condition,
                           observer_comment=comment, retrieved_at=_now())


def _trip():
    return SimpleNamespace(
        departure_time=datetime(2026, 10, 20, 9, 0),
        route_origin="Cabuyao", route_destination="Talim",
    )


# --- the rule ---------------------------------------------------------------

def test_rough_raises_safe_to_caution():
    result = ss.apply_operator_observation(_assessment(ss.STATUS_SAFE), _obs("rough"))
    assert result.overall_status == ss.STATUS_CAUTION
    assert result.recommendation == ss.RECOMMENDATION_CAUTION
    assert "rough water" in result.reason


def test_rough_on_caution_stays_caution_and_adds_reason():
    base = _assessment(ss.STATUS_CAUTION, weather=ss.STATUS_CAUTION)
    result = ss.apply_operator_observation(base, _obs("rough"))
    assert result.overall_status == ss.STATUS_CAUTION
    assert result.recommendation == ss.RECOMMENDATION_CAUTION
    assert result.reason.startswith("Base reason.")
    assert "rough water" in result.reason


def test_rough_on_unsafe_stays_unsafe():
    base = _assessment(ss.STATUS_UNSAFE, wind=ss.STATUS_UNSAFE)
    result = ss.apply_operator_observation(base, _obs("rough"))
    assert result.overall_status == ss.STATUS_UNSAFE
    assert result.recommendation == ss.RECOMMENDATION_UNSAFE


def test_rough_never_turns_unavailable_into_a_recommendation():
    base = _assessment(ss.STATUS_UNAVAILABLE, water=ss.STATUS_UNAVAILABLE)
    result = ss.apply_operator_observation(base, _obs("rough"))
    assert result.overall_status == ss.STATUS_UNAVAILABLE
    assert result.recommendation == ss.RECOMMENDATION_UNAVAILABLE
    assert result.reason == "Base reason."


@pytest.mark.parametrize("condition", ["calm", "moderate"])
def test_calm_and_moderate_change_nothing(condition):
    for status in (ss.STATUS_SAFE, ss.STATUS_CAUTION, ss.STATUS_UNSAFE,
                   ss.STATUS_UNAVAILABLE):
        base = _assessment(status)
        result = ss.apply_operator_observation(base, _obs(condition))
        assert result.overall_status == base.overall_status
        assert result.recommendation == base.recommendation
        assert result.reason == base.reason


def test_no_observation_returns_the_same_assessment():
    base = _assessment(ss.STATUS_SAFE)
    assert ss.apply_operator_observation(base, None) is base


def test_observation_is_attached_for_display():
    result = ss.apply_operator_observation(
        _assessment(ss.STATUS_SAFE), _obs("moderate", "Choppy near the pier"))
    assert result.observed_condition == "moderate"
    assert result.observer_comment == "Choppy near the pier"
    assert result.observation_recorded_at is not None


# --- the lookup: only today's manual observation ---------------------------

def _row(condition, days_old=0, source="manual", minutes_old=0):
    when = _now() - timedelta(days=days_old, minutes=minutes_old)
    db.session.add(EnvironmentalReading(
        source=source, location_label="x", water_level_m=11.5,
        observed_condition=condition, retrieved_at=when,
    ))
    db.session.commit()


def test_lookup_returns_todays_observation(app):
    _row("rough")
    found = monitoring_service.get_todays_observation()
    assert found is not None
    assert found.observed_condition == "rough"


def test_lookup_ignores_an_observation_from_a_previous_day(app):
    _row("rough", days_old=2)
    assert monitoring_service.get_todays_observation() is None


def test_lookup_newest_observation_wins(app):
    _row("rough", minutes_old=30)
    _row("calm")
    assert monitoring_service.get_todays_observation().observed_condition == "calm"


def test_lookup_ignores_rows_without_observation_or_not_manual(app):
    _row(None)
    _row("rough", source="llda")
    assert monitoring_service.get_todays_observation() is None


# --- get_assessment() wiring -----------------------------------------------

_WEATHER = "app.services.scheduling_service.monitoring_service.get_dss_weather_conditions"
_WATER = "app.services.scheduling_service.get_current_water_reading"
_OBS = "app.services.scheduling_service.monitoring_service.get_todays_observation"
_EVAL = "app.services.scheduling_service.evaluate_conditions"


def _weather(status="live"):
    return {"status": status, "reading": SimpleNamespace(), "message": None}


def test_get_assessment_applies_rough_observation(app):
    with patch(_WEATHER, return_value=_weather()), \
         patch(_WATER, return_value=None), \
         patch(_EVAL, return_value=_assessment(ss.STATUS_SAFE)), \
         patch(_OBS, return_value=_obs("rough")):
        result = ss.get_assessment()
    assert result.overall_status == ss.STATUS_CAUTION
    assert result.observed_condition == "rough"


def test_get_assessment_without_observation_is_unchanged(app):
    with patch(_WEATHER, return_value=_weather()), \
         patch(_WATER, return_value=None), \
         patch(_EVAL, return_value=_assessment(ss.STATUS_SAFE)), \
         patch(_OBS, return_value=None):
        result = ss.get_assessment()
    assert result.overall_status == ss.STATUS_SAFE
    assert result.observed_condition is None


def test_weather_is_manual_flag_follows_the_weather_status(app):
    for status, expected in (("manual", True), ("live", False)):
        with patch(_WEATHER, return_value=_weather(status)), \
             patch(_WATER, return_value=None), \
             patch(_EVAL, return_value=_assessment(ss.STATUS_SAFE)), \
             patch(_OBS, return_value=None):
            assert ss.get_assessment().weather_is_manual is expected


# --- Safety Alert text ------------------------------------------------------

def test_alert_names_rough_water_when_it_is_the_only_cause(app):
    assessment = ss.apply_operator_observation(
        _assessment(ss.STATUS_SAFE), _obs("rough"))
    alert = ss.build_safety_alert(assessment, _trip())
    assert "due to rough water observed by the operator" in alert["content"]
    assert "Operator Observation: Rough" in alert["content"]


def test_alert_names_both_when_another_condition_is_also_caution(app):
    base = _assessment(ss.STATUS_CAUTION, weather=ss.STATUS_CAUTION)
    assessment = ss.apply_operator_observation(base, _obs("rough"))
    alert = ss.build_safety_alert(assessment, _trip())
    assert "weather conditions and rough water observed by the operator" in alert["content"]


def test_alert_without_observation_is_unchanged(app):
    base = _assessment(ss.STATUS_CAUTION, weather=ss.STATUS_CAUTION)
    alert = ss.build_safety_alert(base, _trip())
    assert "Operator Observation" not in alert["content"]
    assert "rough" not in alert["content"].lower()


def test_alert_on_unsafe_does_not_blame_the_observation(app):
    base = _assessment(ss.STATUS_UNSAFE, wind=ss.STATUS_UNSAFE)
    assessment = ss.apply_operator_observation(base, _obs("rough"))
    alert = ss.build_safety_alert(assessment, _trip())
    assert "due to wind speed conditions" in alert["content"]
    assert "rough water observed" not in alert["content"]


# --- the manual form route --------------------------------------------------

def _post(admin_client, **extra):
    data = {"water_level_m": "11.5"}
    data.update(extra)
    return admin_client.post("/monitoring/add-reading", data=data)


def test_route_rejects_an_invalid_observation(app, admin_client):
    _post(admin_client, observed_condition="stormy")
    assert EnvironmentalReading.query.filter_by(source="manual").count() == 0


def test_route_saves_a_valid_observation_with_comment(app, admin_client):
    _post(admin_client, observed_condition="Rough", observer_comment="Choppy")
    row = EnvironmentalReading.query.filter_by(source="manual").one()
    assert row.observed_condition == "rough"
    assert row.observer_comment == "Choppy"


def test_route_treats_empty_observation_as_not_observed(app, admin_client):
    _post(admin_client, observed_condition="", observer_comment="")
    row = EnvironmentalReading.query.filter_by(source="manual").one()
    assert row.observed_condition is None
    assert row.observer_comment is None


def test_route_truncates_a_long_comment(app, admin_client):
    _post(admin_client, observed_condition="calm", observer_comment="x" * 500)
    row = EnvironmentalReading.query.filter_by(source="manual").one()
    assert len(row.observer_comment) == 300