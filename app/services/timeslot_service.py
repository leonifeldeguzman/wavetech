"""Time slot (Trip) deletion rules for the admin Timetable.

A slot may be deleted only while nothing about passengers hangs off it.
The rule is enforced here, on the backend; the template merely hides the
trash button for slots that would be refused.

Relationships checked (all have a NOT NULL ``trip_id`` pointing at trips):
  * ManifestEntry        - approved / walk-in passengers on the manifest
  * PendingRegistration  - online passengers (pending, approved, rejected,
                           cancelled)
  * Registration         - parent registration of a booking

Nothing here ever deletes or edits those rows. Announcements only carry an
optional, nullable display tag (announcements.trip_id), so they are kept and
just detached from the removed slot.
"""
from __future__ import annotations

from app.extensions import db
from app.models.announcement import Announcement
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration
from app.models.registration import Registration
from app.models.trip import Trip
from app.services import activity_log_service

ACTIVE_PASSENGER_STATUSES = ("pending", "approved")

MSG_HAS_PASSENGERS = (
    "This time slot cannot be deleted because passengers are already "
    "registered. Use Delay Trip or Cancel Trip instead."
)
MSG_HAS_RECORDS = (
    "This time slot cannot be deleted because it still has registration "
    "records (such as cancelled or rejected ones) that must be kept. "
    "Use Cancel Trip instead."
)
MSG_DEPARTED = (
    "This time slot has already departed and is kept as a departure "
    "record, so it cannot be deleted."
)


def _ids_with(model, trip_ids, *extra_filters) -> set[int]:
    if not trip_ids:
        return set()
    rows = (
        db.session.query(model.trip_id)
        .filter(model.trip_id.in_(list(trip_ids)), *extra_filters)
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def undeletable_trip_ids(trips) -> set[int]:
    """Ids (among ``trips``) whose delete button must be hidden. Uses three
    grouped queries, so the timetable page does not query per slot."""
    trips = list(trips)
    ids = [t.id for t in trips]
    blocked = (
        _ids_with(ManifestEntry, ids)
        | _ids_with(PendingRegistration, ids)
        | _ids_with(Registration, ids)
    )
    blocked |= {t.id for t in trips if t.status == "Departed"}
    return blocked


def deletion_block_reason(trip) -> str | None:
    """Return a user-facing reason this slot cannot be deleted, else None."""
    if (
        ManifestEntry.query.filter_by(trip_id=trip.id).first() is not None
        or PendingRegistration.query.filter(
            PendingRegistration.trip_id == trip.id,
            PendingRegistration.status.in_(ACTIVE_PASSENGER_STATUSES),
        ).first() is not None
    ):
        return MSG_HAS_PASSENGERS
    if (
        PendingRegistration.query.filter_by(trip_id=trip.id).first() is not None
        or Registration.query.filter_by(trip_id=trip.id).first() is not None
    ):
        return MSG_HAS_RECORDS
    if trip.status == "Departed":
        return MSG_DEPARTED
    return None


def delete_time_slot(trip: Trip, *, user_id, admin_name) -> None:
    """Delete an eligible slot and write its Activity Log entry in ONE
    transaction. The caller must already have checked
    ``deletion_block_reason`` while holding a row lock on ``trip``.

    The log text is built before the delete so boat/operator/time survive
    in the log. On any error nothing is deleted and nothing is logged.
    """
    details = (
        "Deleted a time slot from the timetable. "
        + activity_log_service.describe_trip(trip)
    )
    try:
        Announcement.query.filter_by(trip_id=trip.id).update(
            {"trip_id": None}, synchronize_session=False
        )
        db.session.delete(trip)
        activity_log_service.log_action(
            user_id=user_id,
            admin_name=admin_name,
            action=activity_log_service.ACTION_TIME_SLOT_DELETED,
            details=details,
            commit=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
