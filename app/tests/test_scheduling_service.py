"""Tests for WaveTech Scheduling Decision-Support.

All tests use synthetic environmental readings. No external Windy/LLDA
requests are made.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.extensions import db
from app.models.manifest_entry import ManifestEntry
from app.models.trip import Trip
from app.services import llda_mock_service, scheduling_service


def _wind(wind=10.0, weather="Sunny", direction="NE", temperature=28.0):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        wind_speed_kmh=wind,
        wind_direction=direction,
        weather_condition=weather,
        temperature_c=temperature,
        recorded_at=now,
        retrieved_at=now,
    )


def _water(level=12.1):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        source="llda_mock",
        station="Central Bay, Cardona, Rizal",
        water_level_m=level,
        recorded_at=now,
    )


def test_safe_conditions_return_proceed(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(), _water())
        assert result.recommendation == scheduling_service.RECOMMENDATION_PROCEED
        assert result.overall_status == scheduling_service.STATUS_SAFE


def test_caution_conditions_return_caution_delay(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(wind=25), _water())
        assert result.recommendation == scheduling_service.RECOMMENDATION_CAUTION
        assert result.wind_speed.status == scheduling_service.STATUS_CAUTION


def test_unsafe_wind_returns_unsafe(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(wind=35), _water())
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNSAFE
        assert result.wind_speed.status == scheduling_service.STATUS_UNSAFE


def test_unsafe_water_level_returns_unsafe(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(), _water(level=14.0))
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNSAFE
        assert result.water_level.status == scheduling_service.STATUS_UNSAFE


def test_multiple_unsafe_conditions_return_unsafe(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(wind=35, weather="Rainy"), _water(level=14.0))
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNSAFE
        assert result.wind_speed.status == scheduling_service.STATUS_UNSAFE
        assert result.weather.status == scheduling_service.STATUS_UNSAFE
        assert result.water_level.status == scheduling_service.STATUS_UNSAFE


def test_missing_required_data_returns_unavailable(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(wind=None), _water())
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE
        assert result.overall_status == scheduling_service.STATUS_UNAVAILABLE


def test_missing_windy_reading_returns_unavailable(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(None, _water())
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE


def test_missing_llda_reading_returns_unavailable(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(), None)
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE


def test_invalid_values_return_unavailable(app):
    with app.app_context():
        result = scheduling_service.evaluate_conditions(_wind(wind=float("nan")), _water(level="bad"))
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE


def test_windy_api_failure_is_gracefully_unavailable(app):
    with app.app_context():
        with patch(
            "app.services.scheduling_service.monitoring_service.get_windy_conditions",
            return_value={
                "status": "unavailable",
                "reading": None,
                "message": "Windy failed",
            },
        ):
            result = scheduling_service.get_assessment()
        assert result.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE


def test_mock_llda_data_works_and_is_labelled(app):
    with app.app_context():
        reading = llda_mock_service.fetch_water_level()
        assert reading.source == "llda_mock"
        assert reading.water_level_m == app.config["LLDA_MOCK_WATER_LEVEL_M"]
        assert reading.recorded_at is not None


def test_assessment_does_not_modify_trip_or_manifest(app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    with app.app_context():
        before_trip = db.session.get(Trip, trip_id)
        before_status = before_trip.status
        before_manifest_count = ManifestEntry.query.filter_by(trip_id=trip_id).count()

        with patch(
            "app.services.scheduling_service.monitoring_service.get_windy_conditions",
            return_value={"status": "live", "reading": _wind(), "message": None},
        ):
            result = scheduling_service.get_assessment()

        db.session.expire_all()
        after_trip = db.session.get(Trip, trip_id)
        after_manifest_count = ManifestEntry.query.filter_by(trip_id=trip_id).count()

        assert result.recommendation == scheduling_service.RECOMMENDATION_PROCEED
        assert after_trip.status == before_status == "Open"
        assert after_manifest_count == before_manifest_count == 0

def test_dashboard_assessment_does_not_change_trip(admin_client, app, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(boat_id=boat_id, status="Open")

    with patch(
        "app.services.scheduling_service.monitoring_service.get_windy_conditions",
        return_value={"status": "live", "reading": _wind(), "message": None},
    ):
        response = admin_client.get(f"/dashboard?trip_id={trip_id}")

    assert response.status_code == 200
    assert b"Scheduling Decision-Support" in response.data
    assert b"PROCEED" in response.data

    with app.app_context():
        trip = db.session.get(Trip, trip_id)
        assert trip.status == "Open"


# ---------------------------------------------------------------------------
# DSS -> Safety Alert generation (scheduling_service.build_safety_alert)
# ---------------------------------------------------------------------------

def _trip(origin="Cabuyao Terminal", destination="Talim Island", departure_time=None):
    return SimpleNamespace(
        route_origin=origin,
        route_destination=destination,
        departure_time=departure_time or datetime(2026, 9, 15, 7, 30),
    )


def test_build_safety_alert_rainy_unsafe_format(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=10.0, weather="Rainy"), _water(level=12.1)
        )
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_UNSAFE

        alert = scheduling_service.build_safety_alert(assessment, _trip())

        assert alert is not None
        assert alert["title"] == "Safety Alert: Unsafe Travel Conditions"
        assert alert["type"] == "Safety Alert"
        assert "Travel Alert" in alert["content"]
        assert "Cabuyao Terminal to Talim Island" in alert["content"]
        assert "September 15, 2026 07:30 AM" in alert["content"]
        assert "UNSAFE due to rainy weather conditions" in alert["content"]
        assert "Weather: Rainy" in alert["content"]
        assert "Wind Speed: 10.0 km/h" in alert["content"]
        assert "Water Level: 12.1 m" in alert["content"]
        assert "prioritize their safety" in alert["content"]


def test_build_safety_alert_cloudy_caution_format(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=10.0, weather="Cloudy"), _water(level=12.1)
        )
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_CAUTION

        alert = scheduling_service.build_safety_alert(assessment, _trip())

        assert alert is not None
        assert alert["title"] == "Safety Alert: Caution on Travel Conditions"
        assert "Travel Advisory" in alert["content"]
        assert "CAUTION/DELAY due to cloudy weather conditions" in alert["content"]
        assert "Weather: Cloudy" in alert["content"]
        assert "remain alert and monitor" in alert["content"]


def test_build_safety_alert_returns_none_for_proceed(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(_wind(), _water())
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_PROCEED
        assert scheduling_service.build_safety_alert(assessment, _trip()) is None


def test_build_safety_alert_returns_none_when_data_unavailable(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(_wind(wind=None), _water())
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_UNAVAILABLE
        assert scheduling_service.build_safety_alert(assessment, _trip()) is None


def test_build_safety_alert_returns_none_without_a_trip(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=35), _water()
        )
        assert scheduling_service.build_safety_alert(assessment, None) is None


def test_build_safety_alert_states_actual_reason_for_unsafe_wind(app):
    """Weather is Sunny (safe); wind alone is unsafe. The alert must not
    claim this was caused by weather."""
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=35, weather="Sunny"), _water(level=12.1)
        )
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_UNSAFE
        assert assessment.weather.status == scheduling_service.STATUS_SAFE

        alert = scheduling_service.build_safety_alert(assessment, _trip())

        assert "UNSAFE due to wind speed conditions" in alert["content"]
        assert "Weather: Sunny" in alert["content"]


def test_build_safety_alert_states_actual_reason_for_unsafe_water(app):
    with app.app_context():
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=10.0, weather="Sunny"), _water(level=14.0)
        )
        assert assessment.recommendation == scheduling_service.RECOMMENDATION_UNSAFE
        assert assessment.weather.status == scheduling_service.STATUS_SAFE

        alert = scheduling_service.build_safety_alert(assessment, _trip())

        assert "due to water level conditions" in alert["content"]
        assert "Water Level: 14.0 m" in alert["content"]


def test_build_safety_alert_uses_actual_trip_and_environmental_values(app):
    with app.app_context():
        trip = _trip(
            origin="Custom Origin",
            destination="Custom Destination",
            departure_time=datetime(2026, 12, 1, 15, 45),
        )
        assessment = scheduling_service.evaluate_conditions(
            _wind(wind=40.0, weather="Rainy"), _water(level=9.0)
        )

        alert = scheduling_service.build_safety_alert(assessment, trip)

        assert "Custom Origin to Custom Destination" in alert["content"]
        assert "December 01, 2026 03:45 PM" in alert["content"]
        assert "Wind Speed: 40.0 km/h" in alert["content"]
        assert "Water Level: 9.0 m" in alert["content"]
