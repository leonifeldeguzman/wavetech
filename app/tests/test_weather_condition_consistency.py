"""Admin and Passenger must show the same weather condition.

Root cause these tests guard against: the Admin Monitoring headline used the
"worst" condition across ALL stations, while the Passenger page used only the
reference station (XXI), so the same observation could read Cloudy on one
side and Sunny on the other. Both now use monitoring_service's reference
reading, and the condition comes from one WMO-code table in weather_service.

No external requests are made.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.models.lake_station import LakeStation
from app.services import monitoring_service, scheduling_service, weather_service


# --- one WMO table, one answer ---------------------------------------------

@pytest.mark.parametrize("code, expected", [
    (0, "Sunny"), (1, "Sunny"),
    (2, "Partly Cloudy"),
    (3, "Cloudy"), (45, "Cloudy"), (48, "Cloudy"),
    (51, "Rainy"), (61, "Rainy"), (65, "Rainy"), (80, "Rainy"), (82, "Rainy"),
    (95, "Thunderstorm"), (96, "Thunderstorm"), (99, "Thunderstorm"),
    (None, None), (999, None), ("abc", None),
])
def test_wmo_code_maps_to_one_condition(code, expected):
    assert weather_service.normalize_weather_code(code) == expected


def test_fetch_uses_weather_code_not_cloud_cover(app):
    stations = [SimpleNamespace(station_no="XXI", latitude=14.34, longitude=121.28)]
    payload = [{
        "latitude": 14.34, "longitude": 121.28,
        "current": {"time": 1760000000, "temperature_2m": 30.0,
                    "wind_speed_10m": 10, "wind_direction_10m": 90,
                    "weather_code": 2},
    }]
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    with patch.object(weather_service.requests, "get", return_value=response) as get:
        result = weather_service.fetch_station_weather(stations)
    assert "weather_code" in get.call_args.kwargs["params"]["current"]
    assert result[0].weather_condition == "Partly Cloudy"


# --- weather condition vs. environmental (safety) condition ----------------

def _reading(weather, wind=10.0):
    return SimpleNamespace(wind_speed_kmh=wind, weather_condition=weather)


def test_environmental_condition_is_weather_only(app):
    # Wind in the caution/unsafe range must not change the weather-based status.
    for wind in (5, 25, 45, None):
        env = scheduling_service.get_environmental_condition(_reading("Sunny", wind=wind))
        assert env["status"] == scheduling_service.STATUS_SAFE
    assert (scheduling_service.get_environmental_condition(_reading("Cloudy", wind=5))["status"]
            == scheduling_service.STATUS_CAUTION)
    assert (scheduling_service.get_environmental_condition(_reading("Rainy", wind=5))["status"]
            == scheduling_service.STATUS_UNSAFE)
    assert (scheduling_service.get_environmental_condition(None)["status"]
            == scheduling_service.STATUS_UNAVAILABLE)


def test_finer_labels_are_not_unavailable_in_safety_rules(app):
    cls = scheduling_service._classify_weather
    assert cls("Partly Cloudy").status != scheduling_service.STATUS_UNAVAILABLE
    assert cls("Thunderstorm").status == scheduling_service.STATUS_UNSAFE
    assert cls(None).status == scheduling_service.STATUS_UNAVAILABLE


# --- Admin and Passenger read the same observation -------------------------

def _seed_fresh_batch(sunny_ref=True):
    """Reference station XXI Sunny, another station Cloudy, fresh timestamps
    so no HTTP request is made."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref = LakeStation(station_no="XXI", name="Cardona", latitude=14.34, longitude=121.28)
    other = LakeStation(station_no="I", name="Other", latitude=14.5, longitude=121.3)
    db.session.add_all([ref, other])
    db.session.flush()
    for station, cond in ((ref, "Sunny"), (other, "Cloudy")):
        db.session.add(EnvironmentalReading(
            source=monitoring_service.SOURCE_OPEN_METEO,
            station_id=station.id,
            location_label=f"{station.name} (Station {station.station_no})",
            wind_speed_kmh=10.0, wind_direction="E",
            weather_condition=cond, temperature_c=30.0,
            recorded_at=now, retrieved_at=now,
        ))
    db.session.commit()


def test_admin_and_passenger_use_same_reference_reading(app):
    _seed_fresh_batch()
    result = monitoring_service.get_station_weather()
    admin_reading = monitoring_service.pick_reference_reading(result)
    passenger = monitoring_service.get_dss_weather_conditions()

    assert admin_reading is not None
    assert admin_reading.id == passenger["reading"].id
    assert admin_reading.weather_condition == "Sunny"   # not the Cloudy station
    assert admin_reading.retrieved_at == passenger["reading"].retrieved_at


def test_admin_headline_matches_passenger_page(app, admin_client, client):
    _seed_fresh_batch()
    with patch.object(monitoring_service, "get_llda_conditions",
                      return_value={"status": "unavailable", "reading": None,
                                    "message": None}):
        admin_html = admin_client.get("/monitoring").get_data(as_text=True)
    passenger_html = client.get("/passenger/").get_data(as_text=True)

    # Admin headline card names the reference station and its condition.
    assert "Cardona" in admin_html and "Station XXI" in admin_html
    admin_card = admin_html.split("monitoring-summary-label", 1)[1]
    assert "Sunny" in admin_card.split("monitoring-summary-source", 1)[0]
    # Passenger weather card shows the same condition.
    passenger_card = passenger_html.split("home-environment-label", 1)[1]
    assert "Sunny" in passenger_card.split("home-environment-item", 1)[0]
