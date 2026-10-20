from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime
from app.services import monitoring_service

from app.blueprints.passenger import routes as passenger_routes

FIXED_NOW = datetime(2026, 10, 20, 10, 0)
PATCH_TARGET = "app.blueprints.passenger.routes.ph_now"


def _trip(departure, new_departure=None):
    return SimpleNamespace(departure_time=departure, new_departure_time=new_departure)


def test_past_trip_has_passed():
    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        trip = _trip(datetime(2026, 10, 20, 9, 0))
        assert passenger_routes._has_departure_time_passed(trip) is True


def test_future_trip_has_not_passed():
    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        trip = _trip(datetime(2026, 10, 20, 11, 0))
        assert passenger_routes._has_departure_time_passed(trip) is False


def test_delayed_trip_uses_new_departure_time():
    # Original 9:00 has passed, but the delay moved it to 2:00 PM.
    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        trip = _trip(datetime(2026, 10, 20, 9, 0), datetime(2026, 10, 20, 14, 0))
        assert passenger_routes._has_departure_time_passed(trip) is False


def test_delayed_trip_whose_new_time_also_passed():
    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        trip = _trip(datetime(2026, 10, 20, 7, 0), datetime(2026, 10, 20, 9, 30))
        assert passenger_routes._has_departure_time_passed(trip) is True


def test_trip_at_exactly_now_has_not_passed():
    # The list query uses >= now, so the helper must agree at the boundary.
    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        trip = _trip(FIXED_NOW)
        assert passenger_routes._has_departure_time_passed(trip) is False

def test_list_hides_past_trip_and_shows_future_trip(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    past_trip = make_trip(boat_id=boat_id, status="Open",
                          departure_time=datetime(2026, 10, 20, 8, 0))
    future_trip = make_trip(boat_id=boat_id, status="Open",
                            departure_time=datetime(2026, 10, 20, 15, 0))

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        resp = client.get("/api/trips")

    trip_ids = [t["trip_id"] for t in resp.get_json()["data"]]
    assert future_trip in trip_ids
    assert past_trip not in trip_ids


def test_list_keeps_delayed_trip_whose_new_time_is_ahead(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    delayed_trip = make_trip(
        boat_id=boat_id,
        status="Delayed",
        departure_time=datetime(2026, 10, 20, 9, 0),       # already passed
        new_departure_time=datetime(2026, 10, 20, 14, 0),  # still ahead
    )

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        resp = client.get("/api/trips")

    trip_ids = [t["trip_id"] for t in resp.get_json()["data"]]
    assert delayed_trip in trip_ids


def test_list_hides_delayed_trip_whose_new_time_has_passed(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    delayed_trip = make_trip(
        boat_id=boat_id,
        status="Delayed",
        departure_time=datetime(2026, 10, 20, 7, 0),
        new_departure_time=datetime(2026, 10, 20, 9, 30),  # also passed
    )

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        resp = client.get("/api/trips")

    trip_ids = [t["trip_id"] for t in resp.get_json()["data"]]
    assert delayed_trip not in trip_ids


def test_detail_of_past_trip_does_not_accept_registration(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    past_trip = make_trip(boat_id=boat_id, status="Open",
                          departure_time=datetime(2026, 10, 20, 8, 0))

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        resp = client.get(f"/api/trips/{past_trip}")

    data = resp.get_json()["data"]  # ADAPT if the detail response is shaped differently
    assert data["accepts_registration"] is False


UNAVAILABLE_WEATHER = {"status": "unavailable", "reading": None, "message": None}


def _load_home(client):
    with patch(PATCH_TARGET, return_value=FIXED_NOW), \
         patch.object(monitoring_service, "get_dss_weather_conditions",
                      return_value=UNAVAILABLE_WEATHER):
        return client.get("/passenger/").get_data(as_text=True)


def test_home_card_shows_new_time_for_delayed_trip(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    make_trip(
        boat_id=boat_id,
        status="Delayed",
        departure_time=datetime(2026, 10, 20, 9, 0),       # original, already passed
        new_departure_time=datetime(2026, 10, 20, 14, 0),  # still ahead
    )

    html = _load_home(client)

    assert "2:00PM" in html
    assert "9:00AM" not in html


def test_home_card_shows_original_time_when_not_delayed(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    make_trip(boat_id=boat_id, status="Open",
              departure_time=datetime(2026, 10, 20, 15, 0))

    html = _load_home(client)

    assert "3:00PM" in html

def test_api_summary_shows_new_time_for_delayed_trip(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    trip_id = make_trip(
        boat_id=boat_id,
        status="Delayed",
        departure_time=datetime(2026, 10, 20, 9, 0),
        new_departure_time=datetime(2026, 10, 20, 14, 0),
    )

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        data = client.get("/api/trips").get_json()["data"]

    row = next(t for t in data if t["trip_id"] == trip_id)
    assert row["display_time"] == "2:00 PM"
    assert row["departure_time"] == "9:00 AM"   # the API field is unchanged


def test_schedule_page_shows_new_time_for_delayed_trip(client, make_boat, make_trip):
    boat_id = make_boat(capacity=5)
    make_trip(
        boat_id=boat_id,
        status="Delayed",
        departure_time=datetime(2026, 10, 20, 9, 0),
        new_departure_time=datetime(2026, 10, 20, 14, 0),
    )

    with patch(PATCH_TARGET, return_value=FIXED_NOW):
        html = client.get("/passenger/schedule").get_data(as_text=True)

    assert "2:00 PM" in html
    assert "9:00 AM" not in html