from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip


def _p(name, age=30, **kw):
    d = {"full_name": name, "age": age, "passenger_type": "Regular", "address": "Bigaa, Cabuyao"}
    d.update(kw)
    return d


def _post(client, trip_id, mode, people):
    return client.post("/passenger/register/submit-party", json={
        "trip_id": trip_id, "mode": mode, "people": people,
        "consent_privacy": True, "consent_truth": True})


def test_group_gets_one_code_each_and_shared_contact(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=5), status="Open")
    r = _post(client, trip_id, "group", [
        _p("Maria Santos", contact_number="+639123456789"),
        _p("Jose Santos", same_as_contact=True),
        _p("Ana Santos", age=9, passenger_type="Student", address="Mamatid, Cabuyao")])
    assert r.status_code == 201
    codes = [x["reference_code"] for x in r.get_json()["data"]["registrations"]]
    assert len(set(codes)) == 3
    rows = PendingRegistration.query.filter_by(trip_id=trip_id).all()
    assert {x.contact_number for x in rows} == {"09123456789"}
    assert all(x.status == "pending" for x in rows)


def test_group_larger_than_seats_is_rejected_entirely(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=2), status="Open")
    r = _post(client, trip_id, "group", [
        _p("A", contact_number="09123456789"), _p("B"), _p("C")])
    assert r.status_code == 409 and r.get_json()["error_code"] == "TRIP_FULL"
    assert PendingRegistration.query.filter_by(trip_id=trip_id).count() == 0


def test_party_limit_is_five(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=10), status="Open")
    people = [_p("P0", contact_number="09123456789")] + [_p(f"P{i}") for i in range(1, 6)]
    assert _post(client, trip_id, "group", people).status_code == 400


def test_group_that_fills_trip_marks_it_full(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=2), status="Open")
    assert _post(client, trip_id, "group",
                 [_p("A", contact_number="09123456789"), _p("B")]).status_code == 201
    assert Trip.query.get(trip_id).status == "Full"


def test_solo_minor_needs_guardian(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=5), status="Open")
    no_guardian = _post(client, trip_id, "solo", [_p("Teen", 15, contact_number="09123456789")])
    assert no_guardian.status_code == 400
    ok = _post(client, trip_id, "solo", [_p("Teen", 15, contact_number="09123456789",
                                            guardian_name="Parent", guardian_contact="09170000000")])
    assert ok.status_code == 201
    reg = PendingRegistration.query.filter_by(trip_id=trip_id).first()
    assert reg.guardian_name == "Parent" and reg.guardian_contact == "09170000000"


def test_group_contact_person_must_be_adult(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=5), status="Open")
    assert _post(client, trip_id, "group",
                 [_p("Kid", 12, contact_number="09123456789"), _p("Mom")]).status_code == 400


def test_consent_required(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat(capacity=5), status="Open")
    r = client.post("/passenger/register/submit-party", json={
        "trip_id": trip_id, "mode": "solo",
        "people": [_p("A", contact_number="09123456789")],
        "consent_privacy": True, "consent_truth": False})
    assert r.status_code == 400