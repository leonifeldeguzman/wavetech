"""Time slot deletion (admin Timetable) and the Activity Log entries for
manifest / trip management.

Run with:
    python -m pytest app/tests/test_time_slot_delete.py -v
"""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models.activity_log import ActivityLog
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration
from app.models.registration import Registration
from app.models.trip import Trip
from app.utils.ph_clock import ph_today


def _slot_time():
    return datetime.combine(ph_today(), time(20, 30))


def _slot(make_trip, **kwargs):
    return make_trip(departure_time=_slot_time(), **kwargs)


def _morning_slot_tomorrow(make_trip, **kwargs):
    """Tomorrow 9:00 AM: inside terminal hours (08:00-17:00), so a delay to
    10:15 is valid, and never expired whatever time the tests run."""
    return make_trip(
        departure_time=datetime.combine(ph_today() + timedelta(days=1), time(9, 0)),
        **kwargs,
    )


def _delete(client, trip_id, follow=False):
    return client.post(f"/manifests/{trip_id}/delete-slot", follow_redirects=follow)


def _trip_exists(app, trip_id):
    with app.app_context():
        return db.session.get(Trip, trip_id) is not None


def _logs(app, action):
    with app.app_context():
        return ActivityLog.query.filter_by(action=action).all()


def _log_count(app):
    with app.app_context():
        return ActivityLog.query.count()


def _add_manifest_entry(app, trip_id):
    with app.app_context():
        db.session.add(ManifestEntry(
            trip_id=trip_id, full_name="Juan Dela Cruz", age=30,
            passenger_type="Regular", source="walkin",
        ))
        db.session.commit()


def _add_pending(app, trip_id, status="pending"):
    with app.app_context():
        db.session.add(PendingRegistration(
            trip_id=trip_id, full_name="Maria Santos", age=25, status=status,
        ))
        db.session.commit()


# --- eligibility ------------------------------------------------------------

def test_unassigned_slot_without_passengers_can_be_deleted(app, admin_client, make_trip):
    trip_id = _slot(make_trip)

    resp = _delete(admin_client, trip_id)

    assert resp.status_code == 302
    assert not _trip_exists(app, trip_id)


def test_assigned_slot_without_passengers_can_be_deleted(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(name="Sea Star", capacity=5)
    trip_id = _slot(make_trip, boat_id=boat_id, operator_name="Pedro Reyes")

    _delete(admin_client, trip_id)

    assert not _trip_exists(app, trip_id)
    log = _logs(app, "Time Slot Deleted")[0]
    assert "Sea Star" in log.details
    assert "Pedro Reyes" in log.details
    assert "8:30 PM" in log.details


def test_success_flash_appears_after_redirect(admin_client, make_trip):
    trip_id = _slot(make_trip)

    resp = _delete(admin_client, trip_id, follow=True)

    assert b"Time slot deleted." in resp.data
    assert f"/manifests/{trip_id}/delete-slot".encode() not in resp.data


def test_slot_with_manifest_entry_cannot_be_deleted(app, admin_client, make_trip):
    trip_id = _slot(make_trip)
    _add_manifest_entry(app, trip_id)

    resp = _delete(admin_client, trip_id, follow=True)

    assert b"passengers are already registered" in resp.data
    assert b"Time slot deleted." not in resp.data
    assert _trip_exists(app, trip_id)
    with app.app_context():
        assert ManifestEntry.query.filter_by(trip_id=trip_id).count() == 1


def test_slot_with_pending_or_approved_registration_cannot_be_deleted(app, admin_client, make_trip):
    for status in ("pending", "approved"):
        trip_id = _slot(make_trip)
        _add_pending(app, trip_id, status=status)

        resp = _delete(admin_client, trip_id, follow=True)

        assert b"passengers are already registered" in resp.data
        assert _trip_exists(app, trip_id)


def test_slot_with_only_historical_registration_is_kept(app, admin_client, make_trip):
    trip_id = _slot(make_trip)
    _add_pending(app, trip_id, status="rejected")

    resp = _delete(admin_client, trip_id, follow=True)

    assert b"cannot be deleted" in resp.data
    assert _trip_exists(app, trip_id)
    with app.app_context():
        assert PendingRegistration.query.filter_by(trip_id=trip_id).count() == 1


def test_slot_with_parent_registration_cannot_be_deleted(app, admin_client, make_trip):
    trip_id = _slot(make_trip)
    with app.app_context():
        db.session.add(Registration(reference_code="WT-2026-000001", trip_id=trip_id))
        db.session.commit()

    _delete(admin_client, trip_id)

    assert _trip_exists(app, trip_id)


def test_departed_slot_cannot_be_deleted(app, admin_client, make_trip):
    trip_id = _slot(make_trip, status="Departed")

    _delete(admin_client, trip_id)

    assert _trip_exists(app, trip_id)


def test_blocked_or_missing_delete_writes_no_log(app, admin_client, make_trip):
    trip_id = _slot(make_trip)
    _add_manifest_entry(app, trip_id)

    _delete(admin_client, trip_id)
    _delete(admin_client, 999999)

    assert _log_count(app) == 0


def test_requires_login(app, client, make_trip):
    trip_id = _slot(make_trip)

    client.post(f"/manifests/{trip_id}/delete-slot")

    assert _trip_exists(app, trip_id)


def test_deleting_twice_logs_once(app, admin_client, make_trip):
    trip_id = _slot(make_trip)

    _delete(admin_client, trip_id)
    _delete(admin_client, trip_id)

    assert len(_logs(app, "Time Slot Deleted")) == 1


def test_log_records_admin_and_keeps_history(app, admin_client, make_trip):
    trip_id = _slot(make_trip)

    _delete(admin_client, trip_id)

    log = _logs(app, "Time Slot Deleted")[0]
    assert log.admin_name == "Test Admin"
    assert log.created_at is not None
    assert "Deleted a time slot from the timetable." in log.details


# --- timetable page ---------------------------------------------------------

def test_trash_button_only_on_eligible_slots(app, admin_client, make_trip):
    free_id = _slot(make_trip)
    taken_id = _slot(make_trip)
    _add_pending(app, taken_id, status="approved")

    resp = admin_client.get(f"/manifests?date={ph_today().isoformat()}")

    assert f'data-delete-url="/manifests/{free_id}/delete-slot"'.encode() in resp.data
    assert f"/manifests/{taken_id}/delete-slot".encode() not in resp.data
    assert b"Delete Time Slot" in resp.data


# --- Activity Log: manifest and trip management -----------------------------

def test_walkin_manifest_logs_created_once(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(name="Sea Star", capacity=5)
    trip_id = _slot(make_trip, boat_id=boat_id, operator_name="Pedro Reyes")

    admin_client.post(f"/manifests/{trip_id}/add-walkin", data={
        "full_name": "Ana Cruz", "age": "28", "address": "Cabuyao",
        "contact_number": "09123456789", "passenger_type": "Regular",
    })

    logs = _logs(app, "Manifest Created")
    assert len(logs) == 1
    assert logs[0].admin_name == "Test Admin"
    assert "Created a new manifest for a scheduled trip." in logs[0].details
    assert "Sea Star" in logs[0].details and "Pedro Reyes" in logs[0].details


def test_failed_walkin_logs_nothing(app, admin_client, make_trip):
    trip_id = _slot(make_trip)  # no boat -> registration refused

    admin_client.post(f"/manifests/{trip_id}/add-walkin", data={
        "full_name": "Ana Cruz", "age": "28", "passenger_type": "Regular",
    })

    assert _log_count(app) == 0


def test_delay_logs_trip_delayed(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(name="Sea Star")
    trip_id = _morning_slot_tomorrow(make_trip, boat_id=boat_id, operator_name="Pedro Reyes")

    admin_client.post(f"/manifests/{trip_id}/delay", data={
        "delay_reason": "Weather conditions", "new_departure_time": "10:15",
    })

    logs = _logs(app, "Trip Delayed")
    assert len(logs) == 1
    assert "Delayed a scheduled trip." in logs[0].details
    assert "Sea Star" in logs[0].details and "Weather conditions" in logs[0].details


def test_invalid_delay_logs_nothing(app, admin_client, make_trip):
    trip_id = _slot(make_trip)

    admin_client.post(f"/manifests/{trip_id}/delay", data={"delay_reason": ""})

    assert _log_count(app) == 0


def test_cancel_logs_trip_cancelled(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(name="Sea Star")
    trip_id = _morning_slot_tomorrow(make_trip, boat_id=boat_id)

    admin_client.post(
        f"/manifests/{trip_id}/cancel", data={"cancel_reason": "Unsafe water conditions"}
    )

    logs = _logs(app, "Trip Cancelled")
    assert len(logs) == 1
    assert "Cancelled a scheduled trip." in logs[0].details
    assert "Unsafe water conditions" in logs[0].details


def test_manifest_delete_log_keeps_trip_details(app, admin_client, make_boat, make_trip):
    boat_id = make_boat(name="Sea Star")
    trip_id = _slot(make_trip, boat_id=boat_id, operator_name="Pedro Reyes")
    _add_manifest_entry(app, trip_id)
    with app.app_context():
        entry_id = ManifestEntry.query.filter_by(trip_id=trip_id).first().id

    admin_client.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")

    logs = _logs(app, "Manifest Deleted")
    assert len(logs) == 1
    assert "Deleted an existing manifest." in logs[0].details
    assert "Sea Star" in logs[0].details and "Juan Dela Cruz" in logs[0].details