"""Departed and expired trips are read-only records (admin side).

Once a trip's status is Departed, or its scheduled departure (PH date and
time) has passed, its trip details, pending registrations, passenger
manifest and condition assessment are locked on the backend, and every
editing control is absent from the page. Other trips keep working exactly
as before.

Run with:
    python -m pytest app/tests/test_departed_trip_lock.py -v
"""
import re
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip
from app.utils.ph_clock import ph_today

DEPARTED_TEXT = "This trip has departed."


def _noon_today():
    return datetime.combine(ph_today(), time(12, 0))


def _pending(app, trip_id, name="Pending Pax"):
    with app.app_context():
        reg = PendingRegistration(
            trip_id=trip_id, full_name=name, age=30, passenger_type="Regular"
        )
        db.session.add(reg)
        db.session.commit()
        return reg.id


def _entry(app, trip_id, name="Onboard Pax"):
    with app.app_context():
        entry = ManifestEntry(
            trip_id=trip_id, full_name=name, age=30, passenger_type="Regular"
        )
        db.session.add(entry)
        db.session.commit()
        return entry.id


def _expired_trip(make_boat, make_trip):
    """Still 'Open', but its departure was yesterday, so it has expired."""
    return make_trip(
        boat_id=make_boat("MB Expired", 15),
        status="Open",
        departure_time=datetime.combine(
            ph_today() - timedelta(days=1), time(12, 0)
        ),
    )


def _departed_trip(make_boat, make_trip):
    return make_trip(
        boat_id=make_boat("MB Coral", 15),
        status="Departed",
        departure_time=_noon_today(),
    )


def _reg_status(app, reg_id):
    with app.app_context():
        return db.session.get(PendingRegistration, reg_id).status


def _trip_status(app, trip_id):
    with app.app_context():
        return db.session.get(Trip, trip_id).status


def _entry_count(app, trip_id):
    with app.app_context():
        return ManifestEntry.query.filter_by(trip_id=trip_id).count()


# ---- Pending registrations -------------------------------------------------

def test_cannot_approve_pending_on_departed_trip(app, admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)
    reg_id = _pending(app, trip_id)

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve")

    assert _reg_status(app, reg_id) == "pending"
    assert _entry_count(app, trip_id) == 0


def test_cannot_reject_pending_on_departed_trip(app, admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)
    reg_id = _pending(app, trip_id)

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/reject")

    assert _reg_status(app, reg_id) == "pending"


def test_departed_registration_cannot_be_reached_through_another_trip(
    app, admin_client, make_boat, make_trip, morning_clock
):
    """A mismatched URL must not act on a departed trip's registration."""
    departed_id = _departed_trip(make_boat, make_trip)
    open_id = make_trip(boat_id=make_boat("MB Reef", 15), status="Open",
                        departure_time=_noon_today())
    reg_id = _pending(app, departed_id)

    approve = admin_client.post(f"/manifests/{open_id}/pending/{reg_id}/approve")
    reject = admin_client.post(f"/manifests/{open_id}/pending/{reg_id}/reject")

    assert approve.status_code == 404
    assert reject.status_code == 404
    assert _reg_status(app, reg_id) == "pending"
    assert _entry_count(app, open_id) == 0


# ---- Manifest --------------------------------------------------------------

def test_cannot_add_edit_or_delete_manifest_on_departed_trip(
    app, admin_client, make_boat, make_trip
):
    trip_id = _departed_trip(make_boat, make_trip)
    entry_id = _entry(app, trip_id)

    admin_client.post(f"/manifests/{trip_id}/add-walkin", data={
        "full_name": "Walk In", "age": "25", "address": "x",
        "contact_number": "09171234567", "passenger_type": "Regular",
    })
    admin_client.post(f"/manifests/{trip_id}/manifest/{entry_id}/edit", data={
        "full_name": "Changed", "age": "40", "address": "x",
        "contact_number": "09171234567", "passenger_type": "Regular",
    })
    admin_client.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")

    with app.app_context():
        entries = ManifestEntry.query.filter_by(trip_id=trip_id).all()
        assert [e.full_name for e in entries] == ["Onboard Pax"]


def test_cannot_change_status_of_departed_trip(app, admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)

    admin_client.post(f"/manifests/{trip_id}/cancel", data={"cancel_reason": "x"})
    admin_client.post(f"/manifests/{trip_id}/delay", data={
        "delay_reason": "x", "new_departure_time": "15:00",
    })

    assert _trip_status(app, trip_id) == "Departed"


# ---- Assess Condition ------------------------------------------------------

def test_dss_details_refused_for_departed_trip(admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)

    response = admin_client.get(f"/dashboard/dss-details/{trip_id}")

    assert response.status_code == 409
    assert response.get_json()["error"] == DEPARTED_TEXT


def test_dashboard_ignores_departed_trip_in_url(admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)

    html = admin_client.get(f"/dashboard?trip_id={trip_id}").get_data(as_text=True)

    assert 'id="schedulingDetailsModal"' not in html


def test_departed_page_has_no_assess_button(admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert 'id="assessConditionBtn"' not in html
    assert 'class="manifest-assess-btn' not in html


# ---- UI: records stay visible, controls are removed -----------------------

MANAGEMENT_CLASSES = (
    "manifest-approve-btn", "manifest-reject-btn", "manifest-edit-btn",
    "manifest-delete-btn", "manifest-add-btn", "manifest-delay-btn",
    "manifest-cancel-btn", "manifest-save-btn", "manifest-confirm-btn",
)


def _assert_read_only_page(html, banner):
    assert banner in html
    for cls in MANAGEMENT_CLASSES:
        assert f'class="{cls}"' not in html and f'class="btn {cls}"' not in html
    assert 'id="delayModal"' not in html
    assert 'id="cancelModal"' not in html
    assert 'id="walkinModal"' not in html
    assert 'id="discardChangesModal"' not in html
    # Back stays, and it is a plain link with no unsaved-changes tracking.
    assert "Back" in html
    assert not re.search(r"<a[^>]*data-back-link", html)


def test_departed_page_keeps_records_visible_and_removes_controls(
    app, admin_client, make_boat, make_trip
):
    trip_id = _departed_trip(make_boat, make_trip)
    _pending(app, trip_id, "Visible Pending")
    _entry(app, trip_id, "Visible Onboard")

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert "Visible Pending" in html
    assert "Visible Onboard" in html
    assert "Pending registrations are locked" not in html
    assert "The passenger manifest is read-only" not in html
    _assert_read_only_page(html, "This trip has departed. Records are read-only.")


def test_expired_page_is_read_only_with_expired_banner(
    app, admin_client, make_boat, make_trip
):
    trip_id = _expired_trip(make_boat, make_trip)
    _entry(app, trip_id, "Visible Onboard")

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert "Visible Onboard" in html
    _assert_read_only_page(html, "This trip has expired. Records are read-only.")


# ---- Expired trips are locked on the backend too ---------------------------

def test_expired_trip_rejects_every_modification(app, admin_client, make_boat, make_trip):
    trip_id = _expired_trip(make_boat, make_trip)
    reg_id = _pending(app, trip_id)
    entry_id = _entry(app, trip_id)

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve")
    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/reject")
    admin_client.post(f"/manifests/{trip_id}/add-walkin", data={
        "full_name": "Walk In", "age": "25", "address": "x",
        "contact_number": "09171234567", "passenger_type": "Regular",
    })
    admin_client.post(f"/manifests/{trip_id}/manifest/{entry_id}/delete")
    admin_client.post(f"/manifests/{trip_id}/cancel", data={"cancel_reason": "Emergency"})
    admin_client.post(f"/manifests/{trip_id}/delay", data={
        "delay_reason": "Boat maintenance", "new_departure_time": "15:00",
    })
    admin_client.post(f"/manifests/{trip_id}/confirm-departure",
                      data={"audit_confirmed": "yes"})
    admin_client.post(f"/manifests/{trip_id}/assign-boat", data={
        "operator_name": "X", "crew_name": "Y",
        "boat_name": "MB Other", "boat_capacity": "10",
    })

    assert _reg_status(app, reg_id) == "pending"
    assert _entry_count(app, trip_id) == 1
    assert _trip_status(app, trip_id) == "Open"


def test_dss_details_refused_for_expired_trip(admin_client, make_boat, make_trip):
    trip_id = _expired_trip(make_boat, make_trip)

    response = admin_client.get(f"/dashboard/dss-details/{trip_id}")

    assert response.status_code == 409


def test_future_trip_with_earlier_clock_time_stays_editable(
    admin_client, make_boat, make_trip
):
    """Tomorrow at 8:00 AM is earlier on the clock than 'now' but not past."""
    trip_id = make_trip(
        boat_id=make_boat("MB Future", 15), status="Open",
        departure_time=datetime.combine(ph_today() + timedelta(days=1), time(8, 0)),
    )

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert "Records are read-only" not in html
    assert 'class="manifest-save-btn"' in html


def test_departed_page_links_to_its_manifest_record(admin_client, make_boat, make_trip):
    trip_id = _departed_trip(make_boat, make_trip)

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    # If Analytics has a record for the trip, the button targets exactly it;
    # if not, no button and no broken link is rendered.
    if "View Summary Details" in html:
        assert f"/analytics?view={trip_id}" in html
    else:
        assert "/analytics?view=" not in html


# ---- Back button: unsaved-changes tracking is present on editable trips ----

def test_editable_page_has_discard_modal_and_tracked_back(
    admin_client, make_boat, make_trip, morning_clock
):
    trip_id = make_trip(boat_id=make_boat("MB Reef", 15), status="Open",
                        departure_time=_noon_today())

    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert 'id="discardChangesModal"' in html
    assert "Discard Changes" in html
    assert "You have unsaved changes to this trip." in html
    assert re.search(r"<a[^>]*data-back-link", html)
    assert 'id="tripSavedValues"' in html


# ---- Other trips are unaffected --------------------------------------------

def test_open_trip_still_fully_editable(app, admin_client, make_boat, make_trip, morning_clock):
    trip_id = make_trip(boat_id=make_boat("MB Reef", 15), status="Open",
                        departure_time=_noon_today())
    reg_id = _pending(app, trip_id)

    admin_client.post(f"/manifests/{trip_id}/pending/{reg_id}/approve")
    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)

    assert _reg_status(app, reg_id) == "approved"
    assert _entry_count(app, trip_id) == 1
    assert 'id="assessConditionBtn"' in html
    assert "Pending registrations are locked" not in html