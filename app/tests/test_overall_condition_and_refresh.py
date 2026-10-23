"""Manifesting-page Overall Condition indicator + weather refresh interval.

1. The indicator must show the SAME condition as the Dashboard (one
   assessment, one classification), mapped to GOOD / CAUTION / POOR /
   UNAVAILABLE.
2. The Settings refresh interval must really control when weather is
   fetched from the provider (not just when the page reloads). A saved
   interval of 0 ("auto-refresh off") must not disable the stored-reading
   window, or every page view would call the weather provider.

No external requests are made (the provider call is patched and counted).

Run with:
    python -m pytest app/tests/test_overall_condition_and_refresh.py -v
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.lake_station import LakeStation
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service, scheduling_service, settings_service
from app.services.weather_service import StationWeather

WATER = "app.services.scheduling_service.get_current_water_reading"
WEATHER = "app.services.scheduling_service.monitoring_service.get_dss_weather_conditions"


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

# name, water, weather, Dashboard overall_status, indicator badge
CASES = [
    ("safe",             11.5, _weather(),                   "Safe",        "GOOD"),
    ("caution water",    12.8, _weather(),                   "Caution",     "CAUTION"),
    ("caution weather",  11.5, _weather(condition="Cloudy"), "Caution",     "CAUTION"),
    ("unsafe water",     13.5, _weather(),                   "Unsafe",      "POOR"),
    ("unsafe wind",      11.5, _weather(wind=45),            "Unsafe",      "POOR"),
    ("unsafe weather",   11.5, _weather(condition="Rainy"),  "Unsafe",      "POOR"),
    ("no water",         None, _weather(),                   "Unavailable", "UNAVAILABLE"),
    ("no weather",       11.5, NO_WEATHER,                   "Unavailable", "UNAVAILABLE"),
    ("cached weather",   11.5, _weather(status="cached"),    "Unavailable", "UNAVAILABLE"),
    # Unsafe weather + missing water: the Dashboard says Unavailable, so the
    # indicator must too (it must not turn this into POOR on its own).
    ("unsafe wx, no water", None, _weather(condition="Rainy"), "Unavailable", "UNAVAILABLE"),
]


@pytest.mark.parametrize("name, water, weather, dash, badge", CASES, ids=[c[0] for c in CASES])
def test_indicator_matches_dashboard_assessment(app, name, water, weather, dash, badge):
    with patch(WATER, return_value=_water(water)), patch(WEATHER, return_value=weather):
        assessment = scheduling_service.get_assessment()
        condition = scheduling_service.get_current_overall_condition()
        status, label, _ = monitoring_service.get_current_safety(assessment=assessment)

    assert assessment.overall_status == dash
    assert condition["status"] == dash          # same status as the Dashboard
    assert condition["badge"] == badge
    assert condition["summary"]


def test_indicator_reuses_a_given_assessment(app):
    with patch(WATER, return_value=_water(11.5)), patch(WEATHER, return_value=_weather()):
        assessment = scheduling_service.get_assessment()
    # No patches here: passing the assessment must not re-evaluate anything.
    assert scheduling_service.get_current_overall_condition(assessment)["badge"] == "GOOD"


def test_indicator_failure_is_unavailable_not_an_error(app):
    with patch.object(scheduling_service, "get_assessment", side_effect=RuntimeError("boom")):
        condition = scheduling_service.get_current_overall_condition()
    assert condition["badge"] == "UNAVAILABLE"


def test_manifests_page_shows_indicator_and_dashboard_link(admin_client):
    with patch(WATER, return_value=_water(11.5)), patch(WEATHER, return_value=_weather(condition="Cloudy")):
        html = admin_client.get("/manifests").get_data(as_text=True)
    assert "Overall Condition" in html
    assert "CAUTION" in html
    assert "Environmental conditions require attention." in html
    assert "/dashboard#scheduling-decision-support" in html
    assert "View Details" in html


def test_manifests_page_does_not_show_detail_monitoring(admin_client):
    with patch(WATER, return_value=_water(11.5)), patch(WEATHER, return_value=_weather()):
        html = admin_client.get("/manifests").get_data(as_text=True)
    for detail in ("Wind Speed", "Water Level", "Recommendation"):
        assert detail not in html


def test_overall_condition_endpoint_requires_login(client):
    response = client.get("/dashboard/overall-condition")
    assert response.status_code == 302


def test_overall_condition_endpoint_matches_manifests_and_dashboard(admin_client):
    with patch(WATER, return_value=_water(13.5)), patch(WEATHER, return_value=_weather()):
        data = admin_client.get("/dashboard/overall-condition").get_json()
        page = admin_client.get("/manifests").get_data(as_text=True)
    assert data["badge"] == "POOR"
    assert data["status"] == "Unsafe"
    assert "POOR" in page
    assert admin_client.get("/dashboard/overall-condition").headers["Cache-Control"] == "no-store"


# --- refresh interval -> actual provider calls --------------------------------

def _station(app):
    st = LakeStation(station_no="XXI", name="Cardona", latitude=14.3, longitude=121.2)
    db.session.add(st)
    db.session.commit()
    return st


def _fake_fetch(counter):
    def fetch(stations):
        counter.append(1)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return [StationWeather(
            station_no=s.station_no, wind_speed_kmh=10.0, wind_direction="NE",
            weather_condition="Sunny", temperature_c=28.0,
            recorded_at=now, retrieved_at=now, grid_lat=1.0, grid_lon=1.0,
        ) for s in stations]
    return fetch


def _age_stored_weather(seconds):
    """Pretend the stored Open-Meteo batch was fetched `seconds` ago."""
    for row in EnvironmentalReading.query.filter_by(source="open_meteo").all():
        row.retrieved_at = row.retrieved_at - timedelta(seconds=seconds)
    db.session.commit()


@pytest.mark.parametrize("interval", [60, 300])
def test_weather_is_fetched_only_when_the_interval_expires(app, interval):
    _station(app)
    settings_service.update_refresh_interval(interval)
    calls = []
    with patch("app.services.weather_service.fetch_station_weather", _fake_fetch(calls)):
        monitoring_service.get_station_weather()
        monitoring_service.get_station_weather()           # same window -> stored reading
        assert len(calls) == 1

        _age_stored_weather(interval - 5)                  # just inside the window
        monitoring_service.get_station_weather()
        assert len(calls) == 1

        _age_stored_weather(10)                            # now past the interval
        monitoring_service.get_station_weather()
        assert len(calls) == 2


def test_interval_zero_does_not_call_provider_on_every_request(app):
    """0 = auto-refresh off in the UI. It used to mean 'nothing is ever fresh',
    so every page view/poll fetched from the weather provider."""
    _station(app)
    settings_service.update_refresh_interval(0)
    calls = []
    with patch("app.services.weather_service.fetch_station_weather", _fake_fetch(calls)):
        for _ in range(5):
            monitoring_service.get_station_weather()
    assert len(calls) == 1


def test_cache_ttl_follows_setting_and_falls_back_when_off(app):
    settings_service.update_refresh_interval(300)
    assert settings_service.get_data_cache_ttl_seconds() == 300
    settings_service.update_refresh_interval(0)
    assert settings_service.get_data_cache_ttl_seconds() == app.config["REFRESH_INTERVAL_SECONDS"]


# --- Manifesting condition alert (rendered markup; behavior is client-side) ----

@pytest.mark.parametrize("water, weather", [
    (11.5, _weather()),                       # GOOD
    (12.8, _weather()),                       # CAUTION
    (13.5, _weather()),                       # POOR
])
def test_alert_is_never_visible_on_initial_page_load(admin_client, water, weather):
    """The alert element is always rendered hidden; only a later poll that
    sees a condition change may show it (so a page load while CAUTION/POOR
    shows just the indicator)."""
    with patch(WATER, return_value=_water(water)), patch(WEATHER, return_value=weather):
        html = admin_client.get("/manifests").get_data(as_text=True)
    assert html.count('id="condition-alert"') == 1          # one alert element only
    tag = html.split('id="condition-alert"', 1)[1].split(">", 1)[0]
    assert "hidden" in tag
    assert 'role="alert"' in html
    assert 'id="condition-alert-dismiss"' in html
    assert '<button type="button" class="condition-alert-dismiss"' in html   # real, keyboard-focusable button


def test_indicator_carries_initial_status_for_change_detection(admin_client):
    with patch(WATER, return_value=_water(12.8)), patch(WEATHER, return_value=_weather()):
        html = admin_client.get("/manifests").get_data(as_text=True)
    assert 'data-initial-status="Caution"' in html


def test_alert_links_to_existing_decision_support_route(admin_client):
    with patch(WATER, return_value=_water(13.5)), patch(WEATHER, return_value=_weather()):
        html = admin_client.get("/manifests").get_data(as_text=True)
    link = html.split('id="condition-alert-link"', 1)[1].split(">", 1)[0]
    assert 'href="/dashboard#scheduling-decision-support"' in link


def test_alert_wording_is_advisory_only(app):
    import os
    path = os.path.join(app.root_path, "templates", "partials", "overall_condition.html")
    source = open(path, encoding="utf-8").read().lower()
    for required in (
        "current environmental conditions require monitoring.",
        "current conditions may affect ferry operations.",
        "view decision support", "review decision support", "dismiss",
    ):
        assert required in source
    for forbidden in ("cancelled", "canceled", "do not allow", "boarding prohibited",
                      "prohibited", "do not depart"):
        assert forbidden not in source


def test_alert_uses_the_existing_poll_not_a_second_one(app):
    import os
    path = os.path.join(app.root_path, "templates", "partials", "overall_condition.html")
    source = open(path, encoding="utf-8").read()
    assert source.count("setInterval(") == 1
    assert source.count("fetch(") == 1
    assert "location.reload" not in source


def test_alert_never_changes_trips_or_manifests(admin_client):
    """Advisory only: viewing the page with a POOR condition changes nothing."""
    from app.models.trip import Trip
    before = Trip.query.count()
    with patch(WATER, return_value=_water(13.5)), patch(WEATHER, return_value=_weather()):
        admin_client.get("/manifests")
        admin_client.get("/dashboard/overall-condition")
    assert Trip.query.count() == before
