"""Trip Information helpers for Manifest Management: input cleaning,
capacity / overload validation and history-based suggestions.

Design notes
------------
* Operator, Crew and Boat are free text. Nothing is hardcoded and nothing has
  to be registered first; suggestions come from values already saved.
* Boat stays the single source of truth for a trip's boat and capacity, so
  every existing reader of ``trip.boat.name`` / ``trip.boat.capacity``
  (analytics, passenger portal, dashboard) keeps working unchanged. Saving a
  boat name + capacity finds the matching Boat row or creates one. Existing
  Boat rows are never edited, so other trips keep their own capacity.
"""
import re

from app.extensions import db
from app.models.boat import Boat
from app.models.manifest_entry import ManifestEntry
from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip

# Limits mirror the column sizes (Boat.name 50, Trip.crew_name / operator_name 100)
FIELD_LIMITS = {"operator_name": 100, "crew_name": 100, "boat_name": 50}
FIELD_LABELS = {"operator_name": "Operator", "crew_name": "Crew", "boat_name": "Boat"}

MIN_CAPACITY = 1
MAX_CAPACITY = 500  # sanity ceiling against typos (e.g. 5000); adjust if needed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def clean_name(raw):
    """Trim and collapse internal whitespace."""
    return re.sub(r"\s+", " ", (raw or "")).strip()


def parse_capacity(raw):
    """Return (value, error). Empty input -> (None, None)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    if re.fullmatch(r"-[0-9]+(\.[0-9]+)?", text):
        return None, "Capacity cannot be negative."
    if not re.fullmatch(r"[0-9]+", text):
        return None, "Capacity must be a whole number (digits only)."
    value = int(text)
    if value < MIN_CAPACITY:
        return None, "Capacity must be at least 1."
    if value > MAX_CAPACITY:
        return None, f"Capacity cannot exceed {MAX_CAPACITY}."
    return value, None


def overload_message(passenger_count, capacity):
    excess = passenger_count - capacity
    return (
        f"Overloaded: this trip already has {passenger_count} "
        f"passenger{'s' if passenger_count != 1 else ''} on the manifest, "
        f"which exceeds a capacity of {capacity}. Remove {excess} "
        f"passenger{'s' if excess != 1 else ''} or enter a capacity of at "
        f"least {passenger_count}."
    )


def validate_trip_details(form, passenger_count):
    """Validate the Trip Information form.

    Returns (values, errors):
      values - cleaned strings to save, or to echo back when there are errors
               (``values["_capacity_int"]`` is the parsed capacity or None)
      errors - {field_name: message}; empty when everything is valid
    """
    values = {
        "operator_name": clean_name(form.get("operator_name")),
        "crew_name": clean_name(form.get("crew_name")),
        "boat_name": clean_name(form.get("boat_name")),
        "boat_capacity": (form.get("boat_capacity") or "").strip(),
    }
    errors = {}

    for field, limit in FIELD_LIMITS.items():
        if len(values[field]) > limit:
            errors[field] = (
                f"{FIELD_LABELS[field]} name is too long (max {limit} characters)."
            )

    capacity, capacity_error = parse_capacity(values["boat_capacity"])
    if capacity_error:
        errors["boat_capacity"] = capacity_error
    elif capacity is not None and not values["boat_name"]:
        errors.setdefault("boat_name", "Enter the boat name for this capacity.")
    elif capacity is None and values["boat_name"]:
        errors["boat_capacity"] = "Enter the boat\u2019s capacity."
    elif capacity is not None and capacity < passenger_count:
        errors["boat_capacity"] = overload_message(passenger_count, capacity)

    values["_capacity_int"] = capacity
    return values, errors


# ---------------------------------------------------------------------------
# Boat find-or-create (no separate registration step)
# ---------------------------------------------------------------------------

def find_or_create_boat(name, capacity):
    """Return the Boat row for (name, capacity), creating it when new.

    Name matching is case-insensitive and reuses the spelling already saved
    for that name. Rows are never modified, only added, so trips on the same
    boat name with a different capacity are not affected.
    """
    same_name = Boat.query.filter(db.func.lower(Boat.name) == name.lower())
    exact = same_name.filter(Boat.capacity == capacity).first()
    if exact:
        return exact
    existing = same_name.order_by(Boat.id).first()
    boat = Boat(name=existing.name if existing else name, capacity=capacity)
    db.session.add(boat)
    db.session.flush()
    return boat


STATUS_UNASSIGNED = "Unassigned"


def is_assignment_complete(trip):
    """True only when the trip has a boat, an operator AND a crew.

    Operator and crew are free text (typed or picked from the suggestions),
    so a value of only spaces counts as missing, the same as empty.
    """
    return bool(
        trip.boat_id is not None
        and (trip.operator_name or "").strip()
        and (trip.crew_name or "").strip()
    )


# ---------------------------------------------------------------------------
# Occupancy and the derived "Full" status
# ---------------------------------------------------------------------------
#
# One definition of "a seat is taken", shared by the timetable, the manifest
# page, the banner, the Add buttons and the passenger portal:
#
#   occupied seats = ManifestEntry rows (approved online + walk-in)
#                  + PendingRegistration rows still "pending"
#
# This is exactly the passenger portal's rule (_reserved_seats_count). Rejected
# and cancelled registrations never count, and an approved registration is
# not counted twice (approving it creates its ManifestEntry).

def occupied_seats(trip_id):
    manifest = ManifestEntry.query.filter_by(trip_id=trip_id).count()
    pending = PendingRegistration.query.filter_by(
        trip_id=trip_id, status="pending"
    ).count()
    return manifest + pending


def occupied_seats_by_trip(trip_ids):
    """{trip_id: occupied seats} in two grouped queries (no query per slot)."""
    trip_ids = list(trip_ids)
    counts = {tid: 0 for tid in trip_ids}
    if not trip_ids:
        return counts
    for model, extra in (
        (ManifestEntry, ()),
        (PendingRegistration, (PendingRegistration.status == "pending",)),
    ):
        rows = (
            db.session.query(model.trip_id, db.func.count(model.id))
            .filter(model.trip_id.in_(trip_ids), *extra)
            .group_by(model.trip_id)
            .all()
        )
        for tid, n in rows:
            counts[tid] += n
    return counts


def is_at_capacity(capacity, occupied):
    """True when a known capacity is reached or exceeded."""
    return bool(capacity) and occupied >= capacity


def _status_when_not_full(trip):
    """What a stored "Full" trip goes back to once a seat is free: the same
    rule the passenger portal uses when a booking is cancelled (a trip that
    was delayed before it filled up goes back to Delayed, not Open)."""
    return "Delayed" if trip.new_departure_time else "Open"


def effective_status(trip, passenger_count=None):
    """Status to show and to act on for a trip.

    * Stored "Open" is only really open once boat, operator and crew are all
      assigned; until then it is "Unassigned".
    * "Full" is derived from the live seat count against the boat's capacity,
      so the timetable and the manifest page can never disagree and a stale
      stored value (after a delete, a cancellation or a capacity change)
      cannot show. Only Open (or an already stored Full) can become / stop
      being Full: Boarding, Delayed, Cancelled and Departed are lifecycle
      statuses and are returned unchanged. A trip with no boat has no known
      capacity, so its stored status is left alone.

    Derived on every read, so no stored row has to change. ``passenger_count``
    is optional; pass it (see occupied_seats_by_trip) to avoid a query.
    """
    status = trip.status
    if status not in ("Open", "Full"):
        return status

    capacity = trip.boat.capacity if trip.boat else None
    if capacity:
        if passenger_count is None:
            passenger_count = occupied_seats(trip.id)
        if is_at_capacity(capacity, passenger_count):
            status = "Full"
        elif status == "Full":
            status = _status_when_not_full(trip)

    if status == "Open" and not is_assignment_complete(trip):
        return STATUS_UNASSIGNED
    return status


def sync_trip_status(trip, passenger_count=None):
    """Keep the STORED Open/Full in step with capacity (the passenger portal
    reads the stored value). Other statuses are never touched, so Full can
    never overwrite Boarding, Delayed, Cancelled or Departed."""
    capacity = trip.boat.capacity if trip.boat else None
    if capacity is None:
        if trip.status == "Full":
            trip.status = "Open"
        return
    if passenger_count is None:
        passenger_count = occupied_seats(trip.id)
    if trip.status == "Open" and is_at_capacity(capacity, passenger_count):
        trip.status = "Full"
    elif trip.status == "Full" and not is_at_capacity(capacity, passenger_count):
        trip.status = _status_when_not_full(trip)


# ---------------------------------------------------------------------------
# Duplicate passengers on one trip manifest
# ---------------------------------------------------------------------------
#
# A manifest row has no unique passenger / registration id of its own (online
# approvals copy the details; there is no foreign key back), so a person is
# recognised by what the row does hold:
#   * the normalised name (trimmed, inner spaces collapsed, case-insensitive)
#   * plus age and contact number, which make the match "the same person"
#     rather than just "the same name".
# Duplicates are only ever looked for inside ONE trip, so the same person can
# still book a different trip.

MSG_DUPLICATE_PASSENGER = "This passenger is already registered in this trip manifest."
MSG_DUPLICATE_NAME = "A passenger with this name already exists in this manifest."


def normalize_name(raw):
    """'  JUAN   Dela cruz ' -> 'juan dela cruz' (comparison key only)."""
    return clean_name(raw).casefold()


def _contact_key(raw):
    return re.sub(r"\D", "", raw or "")


def find_duplicate_message(trip_id, full_name, age=None, contact_number=None,
                           exclude_entry_id=None):
    """Return the validation message when this passenger is already on the
    trip's manifest, else None. ``exclude_entry_id`` lets an edit ignore the
    row being edited."""
    key = normalize_name(full_name)
    if not key:
        return None
    query = ManifestEntry.query.filter_by(trip_id=trip_id)
    if exclude_entry_id is not None:
        query = query.filter(ManifestEntry.id != exclude_entry_id)

    same_name = [e for e in query.all() if normalize_name(e.full_name) == key]
    if not same_name:
        return None

    contact = _contact_key(contact_number)
    for entry in same_name:
        if (
            age is not None
            and entry.age == age
            and contact
            and _contact_key(entry.contact_number) == contact
        ):
            return MSG_DUPLICATE_PASSENGER
    return MSG_DUPLICATE_NAME


# ---------------------------------------------------------------------------
# Dynamic suggestions (built only from saved data)
# ---------------------------------------------------------------------------

def _dedupe_keep_first(items, key):
    seen, out = set(), []
    for item in items:
        k = key(item).casefold()
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _recent_values(column):
    rows = (
        db.session.query(column)
        .filter(column.isnot(None), column != "")
        .order_by(Trip.departure_time.desc(), Trip.id.desc())
        .limit(2000)
        .all()
    )
    return _dedupe_keep_first([r[0] for r in rows], key=lambda v: v)


def get_suggestions():
    """{"operators": [...], "crews": [...], "boats": [{"name"}]}

    Most recently used first. Boats are suggested by name only: capacity is
    never suggested or pre-filled, it is always typed in for each trip.
    """
    used = (
        db.session.query(Boat.name)
        .join(Trip, Trip.boat_id == Boat.id)
        .order_by(Trip.departure_time.desc(), Trip.id.desc())
        .limit(2000)
        .all()
    )
    unused = db.session.query(Boat.name).order_by(Boat.name).all()
    boats = [{"name": n} for (n,) in list(used) + list(unused)]
    return {
        "operators": _recent_values(Trip.operator_name),
        "crews": _recent_values(Trip.crew_name),
        "boats": _dedupe_keep_first(boats, key=lambda b: b["name"]),
    }