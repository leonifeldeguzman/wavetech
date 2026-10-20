"""Manual weather for the reference station (XXI) stands in for the weather
model only while that is cached or unavailable. No external requests are made.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.models.lake_station import LakeStation
from app.services import monitoring_service, scheduling_service, weather_service


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stations():
    ref = LakeStation(station_no="XXI", name="Cardona", latitude=14.34, longitude=121.28)
    other = LakeStation(station_no="I", name="Other", latitude=14.5, longitude=121.3)
    db.session.add_all([ref, other])
    db.session.commit()
    return ref, other


def _batch(stations, hours_old=0.0, condition="Sunny"):
    when = _now() - timedelta(hours=hours_old)
    for st in stations:
        db.session.add(EnvironmentalReading(
            source=monitoring_service.SOURCE_OPEN_METEO,
            station_id=st.id,
            location_label=f"{st.name} (Station {st.station_no})",
            wind_speed_kmh=10.0, wind_direction="E",
            weather_condition=condition, temperature_c=30.0,
            recorded_at=when, retrieved_at=when,
        ))
    db.session.commit()


def _manual(station, hours_old=0.0, wind=12.0, condition="Cloudy"):
    db.session.add(EnvironmentalReading(
        source="manual",
        station_id=station.id,
        location_label=f"{station.name} (Station {station.station_no})",
        wind_speed_kmh=wind,
        weather_condition=condition,
        retrieved_at=_now() - timedelta(hours=hours_old),
    ))
    db.session.commit()


def _api_refused():
    # ADAPT: if WeatherServiceError needs different arguments, change this.
    return patch.object(
        monitoring_service.weather_service,
        "fetch_station_weather",
        side_effect=weather_service.WeatherServiceError(
            "HTTP 429: Daily API request limit exceeded"
        ),
    )


# --- get_dss_weather_conditions(): who wins --------------------------------

def test_live_model_data_wins_over_manual(app):
    ref, other = _stations()
    _batch([ref, other], hours_old=0)
    _manual(ref)
    with _api_refused():
        result = monitoring_service.get_dss_weather_conditions()
    assert result["status"] == monitoring_service.STATUS_LIVE
    assert result["reading"].source == monitoring_service.SOURCE_OPEN_METEO


def test_cached_model_data_is_replaced_by_recent_manual(app):
    ref, other = _stations()
    _batch([ref, other], hours_old=2)          # stale batch -> fetch -> refused
    _manual(ref)
    with _api_refused():
        result = monitoring_service.get_dss_weather_conditions()
    assert result["status"] == monitoring_service.STATUS_MANUAL
    assert result["reading"].source == "manual"


def test_unavailable_model_data_is_replaced_by_recent_manual(app):
    ref, other = _stations()                    # no batch at all
    _manual(ref)
    with _api_refused():
        result = monitoring_service.get_dss_weather_conditions()
    assert result["status"] == monitoring_service.STATUS_MANUAL


def test_manual_older_than_limit_is_ignored(app):
    ref, other = _stations()
    _manual(ref, hours_old=4)                   # limit is 3 hours
    with _api_refused():
        result = monitoring_service.get_dss_weather_conditions()
    assert result["status"] == monitoring_service.STATUS_UNAVAILABLE
    assert result["reading"] is None


def test_manual_for_another_station_is_ignored(app):
    ref, other = _stations()
    _manual(other)
    with _api_refused():
        result = monitoring_service.get_dss_weather_conditions()
    assert result["status"] == monitoring_service.STATUS_UNAVAILABLE


def test_manual_row_without_wind_or_condition_is_ignored(app):
    ref, other = _stations()
    db.session.add(EnvironmentalReading(
        source="manual", station_id=ref.id, location_label="x",
        water_level_m=11.5, retrieved_at=_now(),
    ))
    db.session.commit()
    assert monitoring_service.get_recent_manual_weather() is None


# --- decision-support -------------------------------------------------------

def test_assessment_accepts_manual_weather(app):
    ref, other = _stations()
    _manual(ref, wind=5.0, condition="Sunny")
    water = SimpleNamespace(water_level_m=11.5, source="manual",
                            recorded_at=None, retrieved_at=_now())
    with _api_refused(), patch(
        "app.services.scheduling_service.get_current_water_reading",
        return_value=water,
    ):
        assessment = scheduling_service.get_assessment()
    assert assessment.wind_speed.status != scheduling_service.STATUS_UNAVAILABLE
    assert assessment.weather.status != scheduling_service.STATUS_UNAVAILABLE
    assert assessment.windy_retrieved_at is not None


def test_assessment_still_unavailable_without_any_weather(app):
    ref, other = _stations()
    water = SimpleNamespace(water_level_m=11.5, source="manual",
                            recorded_at=None, retrieved_at=_now())
    with _api_refused(), patch(
        "app.services.scheduling_service.get_current_water_reading",
        return_value=water,
    ):
        assessment = scheduling_service.get_assessment()
    assert assessment.overall_status == scheduling_service.STATUS_UNAVAILABLE


# --- the new route ----------------------------------------------------------

def test_weather_route_rejects_condition_not_in_admin_settings(app, admin_client):
    _stations()
    admin_client.post("/monitoring/add-weather", data={
        "wind_speed_kmh": "12", "weather_condition": "Volcano",
    })
    assert EnvironmentalReading.query.filter_by(source="manual").count() == 0


def test_weather_route_saves_row_without_water_level(app, admin_client):
    ref, other = _stations()
    admin_client.post("/monitoring/add-weather", data={
        "wind_speed_kmh": "12", "weather_condition": "Cloudy",
        "wind_direction": "NE",
    })
    row = EnvironmentalReading.query.filter_by(source="manual").one()
    assert row.station_id == ref.id
    assert row.water_level_m is None
    assert row.weather_condition == "Cloudy"
    # A weather-only entry must never count as a water reading.
    assert monitoring_service.get_latest_water_reading() is None