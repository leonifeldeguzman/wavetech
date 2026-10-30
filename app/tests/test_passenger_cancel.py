from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip


def _register(client, trip_id, phone="09123456789"):
    r = client.post("/api/passenger/registrations", json={
        "trip_id": trip_id, "full_name": "Juan Dela Cruz", "age": 30,
        "address": "Bigaa, Cabuyao", "contact_number": phone, "passenger_type": "Regular"})
    return r.get_json()["data"]["reference_code"]


def test_cancel_needs_matching_phone(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=3), status="Open")
    code = _register(client, trip_id)
    bad = client.post(f"/passenger/registrations/{code}/cancel", data={"contact_number": "09999999999"})
    assert bad.status_code == 404
    assert PendingRegistration.query.filter_by(reference_code=code).first().status == "pending"


def test_cancel_accepts_plus63_and_frees_seat(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=1), status="Open")
    code = _register(client, trip_id)
    assert Trip.query.get(trip_id).status == "Full"
    ok = client.post(f"/passenger/registrations/{code}/cancel",
                     data={"contact_number": "+639123456789", "reason": "Typo"})
    assert ok.status_code == 200
    assert PendingRegistration.query.filter_by(reference_code=code).first().status == "cancelled"
    assert Trip.query.get(trip_id).status == "Open"
    assert client.post("/api/passenger/registrations", json={
        "trip_id": trip_id, "full_name": "Next Person", "age": 20,
        "address": "x", "contact_number": "09170000000", "passenger_type": "Regular"}).status_code == 201


def test_status_page_masks_personal_details(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=3), status="Open")
    code = _register(client, trip_id)
    html = client.get(f"/passenger/registrations/{code}").get_data(as_text=True)
    assert "J*** C." in html
    assert "Juan Dela Cruz" not in html
    assert "09123456789" not in html
    assert "6789" in html
    assert "Juan Dela Cruz" not in html


def test_unassigned_trip_not_listed_or_registrable(client, make_trip):
    trip_id = make_trip(boat_id=None, status="Open")
    ids = [t["trip_id"] for t in client.get("/api/trips").get_json()["data"]]
    assert trip_id not in ids