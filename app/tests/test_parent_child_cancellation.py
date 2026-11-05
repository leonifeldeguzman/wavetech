import pytest
from datetime import timedelta

from app.extensions import db
from app.models.pending_registration import PendingRegistration
from app.models.manifest_entry import ManifestEntry
from app.models.registration import Registration
from app.models.cancellation_security import VerificationThrottle
from app.models.trip import Trip
from app.services import registration_service as svc
from app.blueprints.passenger import routes as R

PHONE = "09171234567"
LAST4 = "4567"


def person(name, age=30):
    return {"full_name": name, "age": age, "address": "Brgy X, Majayjay",
            "passenger_type": "Regular", "contact_number": PHONE}


def register(client, trip_id, names, mode=None):
    mode = mode or ("solo" if len(names) == 1 else "group")
    r = client.post("/passenger/register/submit-party", json={
        "trip_id": trip_id, "mode": mode,
        "people": [person(n) for n in names],
        "consent_privacy": True, "consent_truth": True})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()["data"]["reference_code"]


def verify(client, ref, digits=LAST4):
    return client.post(f"/passenger/registrations/{ref}/verify",
                       json={"last_four": digits})


def cancel(client, ref, **body):
    return client.post(f"/passenger/registrations/{ref}/cancel", json=body)


def ids(ref):
    return [p.id for p in PendingRegistration.query
            .filter_by(reference_code=ref).order_by(PendingRegistration.id)]


def reserved(trip_id):
    return R._reserved_seats_count(trip_id)


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": svc.ph_now()}
    monkeypatch.setattr(svc, "_now", lambda: state["t"])
    return state


# 1-3 registration + parent/child --------------------------------------
def test_solo_and_group_create_one_parent_each(client, make_open_trip):
    t = make_open_trip()
    solo = register(client, t, ["Ana Cruz"])
    group = register(client, t, ["Ben Lee", "Cara Diaz", "Dan Ong"])
    ps, pg = (Registration.query.filter_by(reference_code=c).one() for c in (solo, group))
    assert (ps.registration_type, len(ps.passengers)) == ("solo", 1)
    assert (pg.registration_type, len(pg.passengers)) == ("group", 3)
    assert {p.reference_code for p in pg.passengers} == {group}
    assert {p.registration_id for p in pg.passengers} == {pg.id}


def test_legacy_inline_solo_path_creates_parent(client, make_open_trip):
    t = make_open_trip()
    r = client.post("/api/passenger/registrations", json={
        "trip_id": t, **{k: v for k, v in person("Eve Roy").items()}})
    assert r.status_code == 201
    code = r.get_json()["data"]["reference_code"]
    assert Registration.query.filter_by(reference_code=code).one().registration_type == "solo"


# 5-7 verification -----------------------------------------------------
def test_wrong_digits_block_cancellation(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["Ana Cruz"])
    assert verify(client, ref, "0000").status_code == 403
    assert cancel(client, ref).status_code == 403
    assert reserved(t) == 1


def test_generic_error_same_for_unknown_reference(client, make_open_trip):
    ref = register(client, make_open_trip(), ["Ana Cruz"])
    a = verify(client, ref, "0000"); b = verify(client, "WT-2026-000000", "0000")
    assert (a.status_code, a.get_json()["message"]) == (b.status_code, b.get_json()["message"])
    assert PHONE not in a.get_data(as_text=True)


def test_cooldown_after_repeated_failures_even_with_correct_digits(client, make_open_trip, clock):
    ref = register(client, make_open_trip(), ["Ana Cruz"])
    for _ in range(svc.REF_MAX_FAILURES - 1):
        assert verify(client, ref, "0000").status_code == 403
    assert verify(client, ref, "0000").status_code == 429
    r = verify(client, ref, LAST4)           # correct, but locked
    assert r.status_code == 429 and "Retry-After" in r.headers
    clock["t"] += svc.LOCKOUT + timedelta(seconds=1)
    assert verify(client, ref, LAST4).status_code == 200


def test_token_not_reusable_across_registrations_or_after_expiry(client, make_open_trip, clock):
    t = make_open_trip()
    a = register(client, t, ["Ana Cruz"]); b = register(client, t, ["Ben Lee"])
    tok = verify(client, a).get_json()["data"]["verification_token"]
    assert cancel(client, b, verification_token=tok).status_code == 403
    clock["t"] += svc.TOKEN_TTL + timedelta(seconds=1)
    with client.session_transaction() as s:
        s.clear()
    assert cancel(client, a, verification_token=tok).status_code == 403


# 8-13 cancellation ----------------------------------------------------
def test_solo_cancellation_releases_one_seat(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["Ana Cruz"])
    verify(client, ref)
    r = cancel(client, ref); assert r.status_code == 200, r.get_json()
    assert reserved(t) == 0
    p = Registration.query.filter_by(reference_code=ref).one()
    assert p.status == "cancelled" and p.cancelled_at
    assert PendingRegistration.query.filter_by(reference_code=ref).count() == 1  # not deleted


def test_full_group_cancellation(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["A One", "B Two", "C Three"])
    verify(client, ref)
    r = cancel(client, ref, cancel_type="full", reason="plans changed")
    assert r.status_code == 200 and r.get_json()["data"]["seats_released"] == 3
    assert reserved(t) == 0
    assert Registration.query.filter_by(reference_code=ref).one().status == "cancelled"


def test_partial_cancellation_keeps_reference_and_other_seats(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["A One", "B Two", "C Three"])
    first = ids(ref)[0]
    verify(client, ref)
    r = cancel(client, ref, passenger_ids=[first])
    assert r.status_code == 200
    assert reserved(t) == 2
    parent = Registration.query.filter_by(reference_code=ref).one()
    assert parent.status == "pending"          # remaining passengers still await approval
    assert {p.reference_code for p in parent.passengers} == {ref}
    s = client.get(f"/api/passenger/registrations/{ref}").get_json()["data"]
    assert s["active_passenger_count"] == 2 and s["passenger_count"] == 3
    assert s["status"] == "pending"


def test_partial_selecting_everyone_cancels_parent(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["A One", "B Two"])
    verify(client, ref)
    cancel(client, ref, passenger_ids=ids(ref))
    assert Registration.query.filter_by(reference_code=ref).one().status == "cancelled"


def test_partial_validation(client, make_open_trip):
    t = make_open_trip()
    ref = register(client, t, ["A One", "B Two"]); other = register(client, t, ["C Three", "D Four"])
    verify(client, ref)
    assert cancel(client, ref, passenger_ids=[]).status_code == 400
    assert cancel(client, ref, cancel_type="partial").status_code == 400
    r = cancel(client, ref, passenger_ids=[ids(other)[0]])      # someone else's passenger
    assert r.status_code == 400
    assert reserved(t) == 4


def test_duplicate_and_repeat_cancellation_prevented(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["A One", "B Two", "C Three"])
    pid = ids(ref)[0]
    tok = verify(client, ref).get_json()["data"]["verification_token"]
    assert cancel(client, ref, passenger_ids=[pid]).status_code == 200
    # replay with the same (now consumed) token
    assert cancel(client, ref, passenger_ids=[pid], verification_token=tok).status_code == 403
    # fresh verification, but passenger already cancelled
    verify(client, ref)
    r = cancel(client, ref, passenger_ids=[pid])
    assert r.status_code == 409 and r.get_json()["error_code"] == "ALREADY_CANCELLED"
    assert reserved(t) == 2


# 14-17 status / privacy ----------------------------------------------
def test_status_payload_never_contains_contact_or_real_names(client, make_open_trip):
    t = make_open_trip(); ref = register(client, t, ["Alice Reyes", "Bob Santos"])
    verify(client, ref); cancel(client, ref, passenger_ids=[ids(ref)[0]])
    for url in (f"/api/passenger/registrations/{ref}",
                f"/api/passenger/registrations/{ref}/passengers",
                f"/api/passenger/registrations/{ref}/cancellations",
                f"/api/passenger/registrations/{ref}/cancellation-eligibility"):
        body = client.get(url).get_data(as_text=True)
        assert PHONE not in body and "09171234567" not in body
        assert "Alice" not in body and "Bob" not in body and "Reyes" not in body
    data = client.get(f"/api/passenger/registrations/{ref}").get_json()["data"]
    assert [p["name"] for p in data["active_passengers"]] == [R._mask_name("Bob Santos")]
    hist = client.get(f"/api/passenger/registrations/{ref}/cancellations").get_json()["data"]
    assert hist["cancellations"][0]["passengers"][0]["name"] == R._mask_name("Alice Reyes")


# 17-18 capacity / manifest -------------------------------------------
def test_capacity_no_double_count_after_approval_and_reopens_when_full(client, make_open_trip):
    t = make_open_trip(capacity=3); ref = register(client, t, ["A One", "B Two", "C Three"])
    assert db.session.get(Trip, t).status == "Full"
    # simulate admin approval of the first passenger: approved + manifest row
    first = PendingRegistration.query.filter_by(reference_code=ref).order_by(PendingRegistration.id).first()
    first_id, first_name = first.id, first.full_name
    first.status = "approved"
    db.session.add(ManifestEntry(trip_id=t, full_name=first_name, age=30, source="online"))
    db.session.commit()
    assert reserved(t) == 3                       # 1 manifest + 2 pending, not 4

    verify(client, ref)
    # approved passenger cannot be cancelled online (token stays unused)
    r = cancel(client, ref, passenger_ids=[first_id])
    assert r.status_code == 409 and r.get_json()["error_code"] == "NOT_CANCELLABLE"
    assert reserved(t) == 3

    # a pending passenger can, with the same still-valid token
    second_id = ids(ref)[1]
    assert cancel(client, ref, passenger_ids=[second_id]).status_code == 200
    assert reserved(t) == 2
    db.session.expire_all()
    assert db.session.get(Trip, t).status == "Open"


def test_status_for_unknown_reference_is_404(client):
    assert client.get("/api/passenger/registrations/WT-2026-999999").status_code == 404