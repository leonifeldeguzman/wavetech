"""A manual weather entry standing in for the model must show on /monitoring
top cards and on Passenger Home (weather AND wind), labeled as manual."""
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from flask import template_rendered

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.models.lake_station import LakeStation
from app.services import monitoring_service

_DSS = "app.services.monitoring_service.get_dss_weather_conditions"
_STATION_WX = "app.services.monitoring_service.get_station_weather"
_LLDA = "app.services.monitoring_service.get_llda_conditions"


@contextmanager
def _captured_context(app):
    recorded = []

    def record(sender, template, context, **extra):
        recorded.append((template.name, context))

    template_rendered.connect(record, app)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(record, app)


def _manual_reading():
    # Not added to the database: the patched service just returns it.
    return EnvironmentalReading(
        source=monitoring_service.SOURCE_MANUAL,
        wind_speed_kmh=18.5,
        wind_direction="NE",
        weather_condition="Cloudy",
        temperature_c=29.0,
        retrieved_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _manual_result():
    return {
        "status": monitoring_service.STATUS_MANUAL,
        "reading": _manual_reading(),
        "message": "Weather entered manually by the operator.",
    }


def _add_reference_station():
    station = LakeStation(
        station_no=monitoring_service.DSS_STATION_NO,
        name="Cardona",
        latitude=14.34247,
        longitude=121.28668,
        coords_source="test",
        coords_verified=True,
    )
    db.session.add(station)
    db.session.commit()
    return station


def test_monitoring_top_cards_show_manual_entry(app, admin_client):
    station = _add_reference_station()
    model_down = {
        "status": monitoring_service.STATUS_UNAVAILABLE,
        "message": "Test: model refused",
        "stations": [{"station": station, "reading": None}],
    }
    llda_down = {"status": monitoring_service.STATUS_UNAVAILABLE,
                 "reading": None, "message": "Test: no LLDA data"}

    with patch(_STATION_WX, return_value=model_down), \
            patch(_LLDA, return_value=llda_down), \
            patch(_DSS, return_value=_manual_result()):
        with _captured_context(app) as recorded:
            response = admin_client.get("/monitoring")

    assert response.status_code == 200
    context = next(c for n, c in recorded if n == "monitoring/index.html")

    assert context["weather_summary"]["source"] == "manual"
    assert context["weather_summary"]["condition"] == "Cloudy"
    assert context["wind_summary"]["source"] == "manual"
    assert context["wind_summary"]["speed"] == 18.5

    html = response.get_data(as_text=True)
    assert html.count("Source: Manual observation") >= 2


def test_passenger_home_shows_manual_weather_and_wind(app, client):
    with patch(_DSS, return_value=_manual_result()):
        response = client.get("/passenger/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Cloudy" in html
    assert "18.5 km/h" in html   # fails if the Wind block rejects 'manual'
    assert html.count("Manual observation") >= 2