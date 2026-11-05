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


def sync_trip_status(trip, passenger_count):
    """Keep Open/Full in step with capacity. Other statuses are untouched."""
    capacity = trip.boat.capacity if trip.boat else None
    if capacity is None:
        if trip.status == "Full":
            trip.status = "Open"
        return
    if trip.status == "Open" and passenger_count >= capacity:
        trip.status = "Full"
    elif trip.status == "Full" and passenger_count < capacity:
        trip.status = "Open"


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