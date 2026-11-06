"""
Passenger Portal API — Phase 1 (this task only):

    GET  /api/trips
    GET  /api/trips/<trip_id>
    POST /api/passenger/registrations

Scope, deliberately, per current task instructions:
    - No QR generation.
    - No notifications.
    - No environmental monitoring.
    - No conversion of PendingRegistration -> ManifestEntry (that is the
      existing admin approval flow in app/blueprints/manifests/routes.py
      and is left untouched).
    - No new passenger-facing authentication.

This blueprint reuses the existing Trip, Boat, and PendingRegistration
models exactly as they are defined elsewhere in the repository. It does
not add any new tables or columns.
"""
from flask import (
    current_app,
    request,
    render_template,
    redirect,
    url_for,
    abort,
    jsonify,
    session
)

from sqlalchemy.exc import IntegrityError

from app.blueprints.passenger import passenger_bp
from app.extensions import db
from app.models.trip import Trip
from app.models.pending_registration import PendingRegistration, generate_reference_code
from app.utils.api_responses import success_response, error_response
from app.models.announcement import Announcement  # noqa: F401
from app.services import announcement_service
from app.services import semaphore_service, unisms_service
from app.services.activity_log_service import log_action, ACTION_REGISTRATION_SMS_SENT, ACTION_PASSENGER_CANCELLED
from app.models.environmental_reading import EnvironmentalReading
from app.services import manifest_service, monitoring_service, scheduling_service, trip_state_service
from sqlalchemy import func
from app.utils.ph_clock import ph_now
from app.services import registration_service as reg_svc
from app.models.manifest_entry import ManifestEntry
import re


REGISTRABLE_STATUSES = {"Open", "Delayed"}
CANCELLABLE_STATUSES = {"pending"}
# ---------------------------------------------------------------------------
# Business rules (documented here since they are not fully explicit in the
# existing manifests blueprint — see ASSUMPTIONS in the final report).
# ---------------------------------------------------------------------------

# A trip is excluded from the passenger-facing schedule listing entirely
# once it reaches one of these statuses (per task Step 2, explicit).
NON_LISTABLE_STATUSES = {"Cancelled", "Departed", "Full"}

# A trip may not receive NEW registrations once it reaches one of these
# statuses (per task Step 6, explicit minimum requirement). "Full" is
# handled separately below so it always maps to the TRIP_FULL error code
# rather than being lumped in with Cancelled/Departed.
STATUS_BLOCKS_REGISTRATION = {"Cancelled", "Departed"}

# Passenger types accepted, taken verbatim from the existing ManifestEntry
# model's comment (app/models/manifest_entry.py). Not invented.
VALID_PASSENGER_TYPES = {"Regular", "Student", "Senior", "PWD", "Child"}

REFERENCE_CODE_MAX_ATTEMPTS = 5
MAX_PARTY = 5
TERMINAL_HOURS = "8 AM – 5 PM daily"


# ---------------------------------------------------------------------------
# Capacity helpers
# ---------------------------------------------------------------------------

def _confirmed_passenger_count(trip_id):
    """Passengers on the manifest (approved online registrations + walk-ins).
    These are the seats that are definitely taken."""
    return ManifestEntry.query.filter_by(trip_id=trip_id).count()


def _pending_registration_count(trip_id):
    """Online registrations still awaiting admin decision."""
    return PendingRegistration.query.filter_by(
        trip_id=trip_id, status="pending"
    ).count()


def _reserved_seats_count(trip_id):
    """Seats taken = confirmed passengers on the manifest only.
    Pending registrations do not hold a seat until they are approved."""
    return _confirmed_passenger_count(trip_id)


def _trip_capacity_info(trip):
    """Returns (capacity, reserved, remaining) — capacity/remaining are
    None when the trip has no boat assigned yet, since capacity cannot
    be verified from the database in that case."""
    if not trip.boat:
        return None, _reserved_seats_count(trip.id), None

    capacity = trip.boat.capacity
    reserved = _reserved_seats_count(trip.id)
    remaining = max(capacity - reserved, 0)
    return capacity, reserved, remaining


def _has_departure_time_passed(trip):
    """A delayed trip leaves at new_departure_time, so that time decides.
    Both times are naive PH wall-clock, so compare against ph_now()."""
    effective = trip.new_departure_time or trip.departure_time
    return effective < ph_now()


def _assigned_upcoming_query():
    """Trips with a boat that have not yet left (home overview, timetable)."""
    effective = func.coalesce(Trip.new_departure_time, Trip.departure_time)
    return (
        Trip.query
        .filter(Trip.boat_id.isnot(None))
        .filter(effective >= ph_now())
    )


def _upcoming_trips_query():
    effective = func.coalesce(Trip.new_departure_time, Trip.departure_time)
    return (
        _assigned_upcoming_query()
        .filter(~Trip.status.in_(NON_LISTABLE_STATUSES))
        .order_by(effective)
    )


def _registrable_trips():
    """Only trips that can take a registration right now (Register page)."""
    return [t for t in _upcoming_trips_query().all() if _accepts_registration(t)[0]]


def normalize_ph_mobile(raw):
    """Any of 09XXXXXXXXX / +639XXXXXXXXX / 9XXXXXXXXX -> '09XXXXXXXXX', else None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("63") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "0" + digits
    return digits if re.fullmatch(r"09\d{9}", digits) else None


def _mask_name(full_name):
    parts = (full_name or "").split()
    if not parts:
        return "—"
    masked = parts[0][0] + "***"
    if len(parts) > 1:
        masked += f" {parts[-1][0]}."
    return masked


def _mask_phone(number):
    digits = re.sub(r"\D", "", str(number or ""))
    return ("•" * 7 + digits[-4:]) if len(digits) >= 4 else "—"


def _accepts_registration(trip):
    """Returns (can_register, error_code, message). error_code/message
    are only meaningful when can_register is False. Returning an
    explicit code here (rather than inferring one from the message text
    later) avoids the two being able to drift out of sync."""
    if trip.status == "Full":
        return False, "TRIP_FULL", "This trip is already full."
    if trip.status in STATUS_BLOCKS_REGISTRATION:
        return False, "TRIP_NOT_AVAILABLE", "This trip is no longer accepting registrations."
    if trip.status not in REGISTRABLE_STATUSES:
        return False, "TRIP_NOT_AVAILABLE", "This trip is not open for registration."
    if _has_departure_time_passed(trip):
        return False, "TRIP_NOT_AVAILABLE", "This trip's departure time has already passed."
    if not trip.boat:
        return False, "TRIP_NOT_AVAILABLE", "This trip does not have a boat assigned yet."
    if manifest_service.effective_status(trip) == manifest_service.STATUS_UNASSIGNED:
        # Stored "Open" but boat, operator or crew is still missing.
        return False, "TRIP_NOT_AVAILABLE", "This trip is not open for registration yet."
    capacity, reserved, remaining = _trip_capacity_info(trip)
    if remaining is not None and remaining <= 0:
        return False, "TRIP_FULL", "This trip is already full."
    return True, None, None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _display_status(trip, reserved=None):
    """Status shown to passengers. Same rule as the Manifests list and the
    Dashboard: Expired once the trip is closed by time, otherwise
    manifest_service.effective_status (Unassigned / Full derived from the
    manifest). The stored trip.status is left untouched."""
    if trip_state_service.is_expired(trip, now=ph_now()):
        return "Expired"
    return manifest_service.effective_status(trip, reserved)


def _serialize_trip_summary(trip):
    capacity, reserved, remaining = _trip_capacity_info(trip)
    can_register, _error_code, block_reason = _accepts_registration(trip)
    shown_departure = trip.new_departure_time or trip.departure_time

    return {
        "trip_id": trip.id,
        "departure_time": (
            trip.departure_time.strftime("%I:%M %p").lstrip("0")
            if trip.departure_time else None
            
        ),
        "departure_date": (
            trip.departure_time.strftime("%B %d, %Y")
            if trip.departure_time else None
        ),
        "display_time": (
            shown_departure.strftime("%I:%M %p").lstrip("0")
            if shown_departure else None
        ),
        "display_date": (
            shown_departure.strftime("%B %d, %Y")
            if shown_departure else None
        ),
        "route_origin": trip.route_origin,
        "route_destination": trip.route_destination,
        "status": _display_status(trip, reserved),
        "boat": (
            {
                "id": trip.boat.id,
                "name": trip.boat.name
            }
            if trip.boat else None
        ),
        "capacity": capacity,
        "current_passenger_count": reserved,
        "remaining_capacity": remaining,
        "accepts_registration": can_register,
        "notice": block_reason,
        "delay_reason": trip.delay_reason,
        "crew_name": trip.crew_name,
        "operator_name": trip.operator_name,
    }


def _serialize_trip_detail(trip):
    data = _serialize_trip_summary(trip)
    data.update({
        "crew_name": trip.crew_name,
        "delay_reason": trip.delay_reason,
        "cancel_reason": trip.cancel_reason,
        "new_departure_time": (
            f"{trip.new_departure_time.strftime('%B')} "
            f"{trip.new_departure_time.day}, "
            f"{trip.new_departure_time.strftime('%Y')} "
            f"{trip.new_departure_time.strftime('%I:%M %p').lstrip('0')}"
            if trip.new_departure_time
            else None
        ),
        "departed_at": trip.departed_at.isoformat() if trip.departed_at else None,
        "confirmed_count": _confirmed_passenger_count(trip.id),
        "pending_count": _pending_registration_count(trip.id),
    })
    return data


# ---------------------------------------------------------------------------
# GET /api/trips
# ---------------------------------------------------------------------------

@passenger_bp.route("/api/trips", methods=["GET"])
def list_trips():
    trips = _upcoming_trips_query().all()
    return success_response(data=[_serialize_trip_summary(t) for t in trips])



# ---------------------------------------------------------------------------
# GET /api/trips/<trip_id>
# ---------------------------------------------------------------------------

@passenger_bp.route("/api/trips/<int:trip_id>", methods=["GET"])
def get_trip(trip_id):
    trip = db.session.get(Trip, trip_id)
    if not trip:
        return error_response("TRIP_NOT_FOUND", "No trip found with this ID.", status=404)
    return success_response(data=_serialize_trip_detail(trip))


# ---------------------------------------------------------------------------
# POST /api/passenger/registrations
# ---------------------------------------------------------------------------

def validate_passenger_fields(payload):
    """Field-level validation for a passenger's own details — full_name,
    age, address, contact_number, passenger_type.

    Shared by the passenger self-registration flow below (which also
    validates trip_id on top of this) and the Admin/Operator manifest
    edit route (app/blueprints/manifests/routes.py::edit_manifest_entry),
    which never lets trip_id change since editing an entry never moves it
    to a different trip. Extracted here — rather than re-implemented in
    the manifests blueprint — so there is exactly one set of rules for
    what a valid passenger record looks like.
    """
    fields = {}

    full_name = payload.get("full_name")
    if not full_name or not str(full_name).strip():
        fields["full_name"] = "This field is required."
    elif len(str(full_name)) > 100:
        fields["full_name"] = "Must be 100 characters or fewer."

    age = payload.get("age")
    if age is None or str(age).strip() == "":
        fields["age"] = "This field is required."
    else:
        try:
            age = int(age)
            if age <= 0 or age > 120:
                fields["age"] = "Must be a realistic age between 1 and 120."
        except (TypeError, ValueError):
            fields["age"] = "Must be a valid integer."

    address = payload.get("address")
    if address is not None and len(str(address)) > 255:
        fields["address"] = "Must be 255 characters or fewer."

    contact_number = payload.get("contact_number")
    if contact_number is not None and len(str(contact_number)) > 20:
        fields["contact_number"] = "Must be 20 characters or fewer."

    passenger_type = payload.get("passenger_type", "Regular")
    if passenger_type not in VALID_PASSENGER_TYPES:
        fields["passenger_type"] = (
            f"Must be one of: {', '.join(sorted(VALID_PASSENGER_TYPES))}."
        )

    cleaned = {
        "full_name": str(full_name).strip() if full_name else None,
        "age": age if not fields.get("age") else None,
        "address": str(address).strip() if address else None,
        "contact_number": str(contact_number).strip() if contact_number else None,
        "passenger_type": passenger_type,
    }
    return fields, cleaned


def _validate_payload(payload):
    fields, cleaned = validate_passenger_fields(payload)

    trip_id = payload.get("trip_id")
    if trip_id is None or str(trip_id).strip() == "":
        fields["trip_id"] = "This field is required."
    else:
        try:
            trip_id = int(trip_id)
            if trip_id <= 0:
                fields["trip_id"] = "Must be a positive integer."
        except (TypeError, ValueError):
            fields["trip_id"] = "Must be a valid integer."

    cleaned["trip_id"] = trip_id if not fields.get("trip_id") else None
    return fields, cleaned


class RegistrationResult:
    """Simple result object returned by _execute_registration(), shared
    between the JSON API route and the HTML form route below so the
    transaction/locking/business logic only exists in one place."""
    def __init__(self, success, error_code=None, message=None, registration=None,
                 data=None, already_registered=False):
        self.success = success
        self.error_code = error_code
        self.message = message
        self.registration = registration
        # Plain-dict snapshot (no ORM objects) built before commit, so the
        # route never has to touch expired ORM instances after the commit.
        self.data = data
        # True when an identical pending registration already existed and
        # was returned instead of creating a second one.
        self.already_registered = already_registered


def _send_registration_confirmation_sms(registration, trip):
    """Best-effort SMS notification after a successful registration.

    Deliberately isolated from the registration transaction: this runs
    only after db.session.commit() has already succeeded above, and any
    failure here (Semaphore down, unexpected exception, logging error)
    must never change the registration outcome already returned to the
    caller. Swallows everything by design.
    """
    try:
        departure_str = (
            trip.departure_time.strftime("%b %d, %Y, %I:%M %p")
            if trip.departure_time else "TBA"
        )
        message = (
            f"Talim Island Ferry: Registration confirmed. Ref: "
            f"{registration.reference_code}. Trip: {trip.route_origin} to "
            f"{trip.route_destination}, departing {departure_str}."
        )
        result = unisms_service.send_sms(registration.contact_number, message)

        # Not logging "skipped_no_number" — a passenger simply not
        # supplying a contact number is routine, not an event worth an
        # audit entry. Sent / failed / skipped_no_config are logged so
        # admins can see delivery issues.
        if result.status != "skipped_no_number":
            log_action(
                user_id=None,
                admin_name="System",
                action=ACTION_REGISTRATION_SMS_SENT,
                details=(
                    f"Reference {registration.reference_code}, "
                    f"trip #{trip.id}, status={result.status}"
                    + (f", error={result.error}" if result.error else "")
                ),
            )
    except Exception:
        current_app.logger.warning(
            "Registration confirmation SMS/logging failed unexpectedly "
            "for reference %s", getattr(registration, "reference_code", "?"),
            exc_info=True,
        )

def _names_already_on_manifest(trip_id, names):
    """Return the submitted names that already belong to a passenger on this
    trip's manifest (approved passengers). Same normalisation as the admin
    side: trimmed, spaces collapsed, case ignored."""
    wanted = {
        manifest_service.normalize_name(n): n
        for n in names
        if manifest_service.normalize_name(n)
    }
    if not wanted:
        return []

    on_manifest = {
        manifest_service.normalize_name(full_name)
        for (full_name,) in (
            db.session.query(ManifestEntry.full_name)
            .filter(ManifestEntry.trip_id == trip_id)
            .all()
        )
    }
    return [original for norm, original in wanted.items() if norm in on_manifest]


def _duplicate_manifest_message(duplicates):
    if len(duplicates) == 1:
        return (
            f"{duplicates[0]} is already registered on this trip's manifest. "
            "Please check your details or choose a different trip."
        )
    return (
        f"{', '.join(duplicates)} are already registered on this trip's manifest. "
        "Please remove them from the list or choose a different trip."
    )


def _execute_registration(trip_id, cleaned):
    """Runs the actual registration transaction: lock trip -> check
    status/capacity -> check manifest duplicates -> create PendingRegistration
    -> commit. Used by both /api/passenger/registrations (JSON) and the
    plain-HTML registration form route, so the business logic is defined
    exactly once."""
    try:
        # Lock the trip row for the duration of this transaction so that
        # two concurrent registration requests for the same trip cannot
        # both read the same "seats remaining" value and both succeed
        # when only one seat is left. Any second concurrent request for
        # the same trip_id blocks here until the first commits/rolls back.
        # NOTE: deliberately using .filter_by(...).with_for_update().first()
        # rather than .with_for_update().get(trip_id) — SQLAlchemy's
        # Query.get() shortcut can silently ignore query modifiers such
        # as with_for_update() in some versions, which would defeat the
        # row lock entirely without raising any error.
        trip = Trip.query.filter_by(id=trip_id).with_for_update().first()

        if not trip:
            db.session.rollback()
            return RegistrationResult(False, "TRIP_NOT_FOUND", "No trip found with this ID.")

        can_register, error_code, block_reason = _accepts_registration(trip)
        if not can_register:
            db.session.rollback()
            return RegistrationResult(False, error_code, block_reason)

        # Same name already on this trip's manifest -> refuse
        duplicates = _names_already_on_manifest(trip.id, [cleaned["full_name"]])
        if duplicates:
            db.session.rollback()
            return RegistrationResult(
                False, "DUPLICATE_PASSENGER", _duplicate_manifest_message(duplicates)
            )

        # One parent registration per transaction (owns the reference code).
        parent = reg_svc.create_parent_registration(trip, "solo")
        if parent is None:
            db.session.rollback()
            return RegistrationResult(
                False,
                "REFERENCE_CODE_GENERATION_FAILED",
                "Could not generate a unique reference code. Please try again.",
            )

        registration = PendingRegistration(
            trip_id=trip.id,
            registration_id=parent.id,
            reference_code=parent.reference_code,
            full_name=cleaned["full_name"],
            age=cleaned["age"],
            address=cleaned["address"],
            contact_number=cleaned["contact_number"],
            passenger_type=cleaned["passenger_type"],
            status="pending",
        )
        db.session.add(registration)
        db.session.flush()

        # The trip row has been locked (SELECT ... FOR UPDATE) since the
        # capacity check above, so no other registration for this trip
        # could have been committed in between — this recomputation just
        # reflects the seat this registration itself just took, to decide
        # whether the trip should now flip to "Full" (mirrors the same
        # pattern already used in app/blueprints/manifests/routes.py).
        capacity, reserved, remaining = _trip_capacity_info(trip)
        if remaining is not None and remaining <= 0 and trip.status != "Full":
            trip.status = "Full"

        db.session.commit()

    except Exception:
        db.session.rollback()
        return RegistrationResult(
            False,
            "REGISTRATION_FAILED",
            "Registration could not be completed due to a server error.",
        )

    # Registration is already committed at this point — SMS/logging below
    # is best-effort and must not affect the result returned to the caller.
    _send_registration_confirmation_sms(registration, trip)

    return RegistrationResult(True, registration=registration)

def _validate_party(payload):
    """Returns (errors, trip_id, people). Error keys look like '0.full_name'."""
    errors = {}
    try:
        trip_id = int(payload.get("trip_id"))
    except (TypeError, ValueError):
        return {"trip_id": "Please choose a trip."}, None, None

    mode = payload.get("mode")
    raw_people = payload.get("people")
    if mode not in ("solo", "group") or not isinstance(raw_people, list):
        return {"people": "Invalid registration data."}, None, None
    if not (1 <= len(raw_people) <= MAX_PARTY):
        return {"people": f"You can register 1 to {MAX_PARTY} passengers at a time."}, None, None
    if mode == "solo" and len(raw_people) != 1:
        return {"people": "Solo registration is for one passenger."}, None, None
    if mode == "group" and len(raw_people) < 2:
        return {"people": "A group needs at least 2 passengers."}, None, None
    if payload.get("consent_privacy") is not True or payload.get("consent_truth") is not True:
        return {"consent": "Please accept both declarations to continue."}, None, None

    contact_in = raw_people[0] if isinstance(raw_people[0], dict) else {}
    contact_phone = normalize_ph_mobile(contact_in.get("contact_number"))
    if not contact_phone:
        errors["0.contact_number"] = "Enter a valid Philippine mobile number."

    people = []
    for i, raw in enumerate(raw_people):
        if not isinstance(raw, dict):
            errors[f"{i}.full_name"] = "Invalid passenger data."
            continue
        merged = dict(raw)
        merged["contact_number"] = contact_phone
        if i > 0:
            if raw.get("same_as_contact"):
                # Shares the contact person's address and mobile number.
                merged["address"] = contact_in.get("address")
            else:
                # Other passengers give their own mobile number.
                own_phone = normalize_ph_mobile(raw.get("contact_number"))
                if own_phone:
                    merged["contact_number"] = own_phone
                else:
                    errors[f"{i}.contact_number"] = "Enter a valid Philippine mobile number."

        fields, cleaned = validate_passenger_fields(merged)
        if not cleaned.get("address"):
            fields["address"] = "Barangay and city/municipality are required."
        elif len(cleaned["address"]) > 100:
            fields["address"] = "Must be 100 characters or fewer."

        cleaned["guardian_name"] = None
        cleaned["guardian_contact"] = None
        age = cleaned.get("age")
        if i == 0 and age is not None and age < 18:
            if mode == "group":
                fields["age"] = "The contact person must be 18 or older."
            else:
                gname = (raw.get("guardian_name") or "").strip()
                gphone = normalize_ph_mobile(raw.get("guardian_contact"))
                if not gname:
                    fields["guardian_name"] = "Guardian name is required for passengers under 18."
                if not gphone:
                    fields["guardian_contact"] = "Enter a valid guardian contact number."
                cleaned["guardian_name"] = gname[:100] or None
                cleaned["guardian_contact"] = gphone

        for key, msg in fields.items():
            errors[f"{i}.{key}"] = msg
        people.append(cleaned)

    return errors, trip_id, people



def _send_party_sms(registrations, trip):
    """Send the confirmation SMS after a party registration is committed.

    - The contact person (first passenger) gets the full confirmation.
    - Every other passenger whose contact number is different from the
      contact person's (i.e. "Same address and contact number" was NOT
      ticked) also gets a confirmation on their own number.
    - Passengers who share the contact person's number, or who share a
      number with each other, are texted once, not repeatedly.

    Best-effort: each message is sent on its own, so one failure never
    blocks the others or changes the registration result.
    """

    if not registrations:
        return

    try:
        shown = trip.new_departure_time or trip.departure_time

        when = (
            shown.strftime("%b %d, %Y, %I:%M %p")
            if shown else "TBA"
        )

        reference_code = registrations[0].reference_code
        passenger_count = len(registrations)
        route = f"{trip.route_origin} to {trip.route_destination}"

        # Solo -> just the passenger.
        # Group -> the contact person on their own line, then everyone listed.
        names = [(r.full_name or "").strip() for r in registrations]
        contact_name = names[0]

        if passenger_count == 1:
            name_block = f"Passenger: {contact_name}\n"
        else:
            passenger_list = "\n".join(
                f"{i}. {name}" for i, name in enumerate(names, start=1)
            )
            name_block = (
                f"Contact Person: {contact_name}\n\n"
                f"Passengers ({passenger_count}):\n"
                f"{passenger_list}\n"
            )

        # (number, message, label for the activity log)
        outgoing = []
        seen_numbers = set()

        contact_number = registrations[0].contact_number
        outgoing.append((
            contact_number,
            (
                f"WAVETECH | Talim Island Ferry\n\n"
                f"Registration submitted successfully!\n\n"
                f"Reference Code: {reference_code}\n"
                f"{name_block}\n"
                f"Trip: {route}\n"
                f"Departure: {when}\n\n"
                f"Please bring a valid ID for verification."
            ),
            f"{passenger_count} passengers",
        ))
        seen_numbers.add(normalize_ph_mobile(contact_number) or contact_number)

        for reg in registrations[1:]:
            number = reg.contact_number
            key = normalize_ph_mobile(number) or number
            if not number or key in seen_numbers:
                continue
            seen_numbers.add(key)
            first_name = (reg.full_name or "").split()[0] if reg.full_name else "Passenger"
            outgoing.append((
                number,
                (
                    f"Talim Island Ferry: {first_name}, you have been "
                    f"registered for a trip. "
                    f"Reference Code: {reference_code}. "
                    f"Trip: {route}, departing {when}. "
                    f"Please bring a valid ID for verification."
                ),
                "co-passenger",
            ))
    except Exception:
        current_app.logger.warning(
            "Party registration SMS could not be prepared",
            exc_info=True
        )
        return

    for number, message, label in outgoing:
        try:
            result = unisms_service.send_sms(number, message)

            if result.status != "skipped_no_number":
                log_action(
                    user_id=None,
                    admin_name="System",
                    action=ACTION_REGISTRATION_SMS_SENT,
                    details=(
                        f"Reference {reference_code}, "
                        f"{label}, "
                        f"trip #{trip.id}, status={result.status}"
                        + (
                            f", error={result.error}"
                            if result.error else ""
                        )
                    )
                )
        except Exception:
            current_app.logger.warning(
                "Party registration SMS failed (%s)", label,
                exc_info=True
            )


def _party_snapshot(trip_id, reference_code, names):
    """Plain, JSON-safe description of a registration. Built from plain
    values only so serialising it can never fail after the commit."""
    return {
        "reference_code": reference_code,
        "trip_id": trip_id,
        "registration_type": "group" if len(names) > 1 else "solo",
        "passenger_count": len(names),
        "passengers": [
            {"full_name": name, "masked_name": _mask_name(name)}
            for name in names
        ],
    }


def _find_existing_identical_party(trip_id, people):
    """Return the rows of a still-pending registration on this trip that
    has the same contact person number and exactly the same passengers
    (name + contact number), or None. Called while the trip row is locked,
    so two concurrent identical submissions (double click, retry after an
    uncertain response) are serialised and the second one finds the first
    one's rows.

    Passengers in a group can now have different contact numbers, so the
    candidates are found through the contact person's number and then
    every row of those reference codes is compared."""
    contact = people[0].get("contact_number")

    def key(name, number):
        return ((name or "").strip().lower(), number or "")

    wanted = sorted(key(p.get("full_name"), p.get("contact_number")) for p in people)

    ref_codes = [
        code for (code,) in (
            db.session.query(PendingRegistration.reference_code)
            .filter_by(trip_id=trip_id, contact_number=contact, status="pending")
            .distinct()
            .all()
        )
    ]
    if not ref_codes:
        return None

    candidates = (
        PendingRegistration.query
        .filter(
            PendingRegistration.trip_id == trip_id,
            PendingRegistration.status == "pending",
            PendingRegistration.reference_code.in_(ref_codes),
        )
        .order_by(PendingRegistration.id.asc())
        .all()
    )

    groups = {}
    for row in candidates:
        groups.setdefault(row.reference_code, []).append(row)

    for rows in groups.values():
        if sorted(key(row.full_name, row.contact_number) for row in rows) == wanted:
            return rows
    return None


def _execute_party_registration(trip_id, people):
    """
    Register a solo passenger or group under ONE shared reference code.

    Transaction:
    1. Lock the selected trip.
    2. Validate trip availability.
    3. Check capacity for the entire party.
    4. Generate one reference code.
    5. Create all passenger records using that code.
    6. Commit the transaction.
    7. Send confirmation SMS separately.
    """

    try:
        # Lock the trip to prevent concurrent registrations
        # from reserving the same remaining seats.
        trip = (
            Trip.query
            .filter_by(id=trip_id)
            .with_for_update()
            .first()
        )

        if not trip:
            db.session.rollback()

            return RegistrationResult(
                False,
                "TRIP_NOT_FOUND",
                "No trip found with this ID."
            )

        # If this exact party is already registered (the first request
        # succeeded but the browser never saw the answer, or the button was
        # clicked twice), acknowledge the existing registration instead of
        # creating a duplicate. This runs BEFORE the availability/capacity
        # checks because the first registration may itself have just filled
        # the trip, which would otherwise make the retry look like "Full".
        existing = _find_existing_identical_party(trip.id, people)

        if existing:
            snapshot = _party_snapshot(
                trip.id,
                existing[0].reference_code,
                [row.full_name for row in existing],
            )
            db.session.rollback()

            return RegistrationResult(
                True,
                registration=existing,
                data=snapshot,
                already_registered=True
            )

        # Refuse anyone whose name is already on this trip's manifest.
        duplicates = _names_already_on_manifest(
            trip.id, [person["full_name"] for person in people]
        )
        if duplicates:
            db.session.rollback()

            return RegistrationResult(
                False,
                "DUPLICATE_PASSENGER",
                _duplicate_manifest_message(duplicates)
            )

        # Check trip availability.
        ok, error_code, message = _accepts_registration(trip)

        if not ok:
            db.session.rollback()

            return RegistrationResult(
                False,
                error_code,
                message
            )

        # Check available seats for the entire party.
        _, _, remaining = _trip_capacity_info(trip)

        if remaining is not None and len(people) > remaining:
            db.session.rollback()

            return RegistrationResult(
                False,
                "TRIP_FULL",
                f"Only {remaining} seat(s) left on this trip."
            )

        # ONE parent registration (and reference code) for everyone.
        parent = reg_svc.create_parent_registration(
            trip, "group" if len(people) > 1 else "solo"
        )

        if parent is None:
            db.session.rollback()

            return RegistrationResult(
                False,
                "REFERENCE_CODE_GENERATION_FAILED",
                "Could not generate a reference code. Please try again."
            )

        reference_code = parent.reference_code

        # Create all passenger records under the parent.
        registrations = []

        for person in people:
            reg = PendingRegistration(
                trip_id=trip.id,
                registration_id=parent.id,
                reference_code=reference_code,
                status="pending",
                **person
            )

            db.session.add(reg)
            registrations.append(reg)

        # Flush before recalculating capacity.
        db.session.flush()

        # Update trip status if all seats have been reserved.
        _, _, remaining = _trip_capacity_info(trip)

        if remaining is not None and remaining <= 0:
            trip.status = "Full"

        # Build the response data from plain values BEFORE the commit.
        # After commit() every ORM instance is expired, so reading it
        # later would trigger new queries that could fail even though the
        # registration is already saved.
        snapshot = _party_snapshot(
            trip.id,
            reference_code,
            [person["full_name"] for person in people],
        )

        # Commit all passenger records together.
        db.session.commit()

    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "Party registration failed"
        )

        return RegistrationResult(
            False,
            "REGISTRATION_FAILED",
            "Registration could not be completed due to a server error."
        )

    # IMPORTANT:
    # SMS is sent AFTER the database commit.
    # SMS failure must never invalidate a successful registration.
    _send_party_sms(registrations, trip)

    return RegistrationResult(
        True,
        registration=registrations,
        data=snapshot
    )

@passenger_bp.route(
    "/passenger/register/submit-party",
    methods=["POST"]
)
def submit_party_registration():

    payload = request.get_json(silent=True) or {}

    # Validate passenger information.
    errors, trip_id, people = _validate_party(payload)

    if errors:
        return jsonify({
            "success": False,
            "message": next(iter(errors.values())),
            "fields": errors
        }), 400

    # Execute registration.
    result = _execute_party_registration(
        trip_id,
        people
    )

    if not result.success:

        status = (
            404
            if result.error_code == "TRIP_NOT_FOUND"
            else 409
            if result.error_code in (
                "TRIP_FULL",
                "TRIP_NOT_AVAILABLE",
                "DUPLICATE_PASSENGER"
            )
            else 500
        )

        return jsonify({
            "success": False,
            "error_code": result.error_code,
            "message": result.message
        }), status

    # Plain-dict snapshot built inside the service function; no ORM objects
    # are read here, so nothing can fail after the registration is saved.
    data = dict(result.data)
    data["already_registered"] = bool(result.already_registered)

    try:
        data["status_url"] = url_for(
            "passenger.registration_status_page",
            reference_code=data["reference_code"]
        )
    except Exception:
        current_app.logger.warning(
            "Could not build status_url for %s",
            data.get("reference_code"),
            exc_info=True
        )

    return jsonify({
        "success": True,
        "message": (
            "This registration was already received."
            if result.already_registered
            else "Registration submitted successfully."
        ),
        "data": data
    }), (200 if result.already_registered else 201)

@passenger_bp.route("/api/passenger/registrations", methods=["POST"])
def create_registration():
    payload = request.get_json(silent=True)
    if payload is None:
        return error_response(
            "VALIDATION_ERROR",
            "Request body must be valid JSON.",
            status=400,
        )

    field_errors, cleaned = _validate_payload(payload)
    if field_errors:
        return error_response(
            "VALIDATION_ERROR",
            "One or more fields are invalid.",
            status=400,
            fields=field_errors,
        )

    result = _execute_registration(cleaned["trip_id"], cleaned)

    if not result.success:
        status = 404 if result.error_code == "TRIP_NOT_FOUND" else (
            409 if result.error_code in (
                "TRIP_FULL", "TRIP_NOT_AVAILABLE", "DUPLICATE_PASSENGER"
            ) else 500
        )
        return error_response(result.error_code, result.message, status=status)

    return success_response(
        message="Registration submitted successfully.",
        data={
            "reference_code": result.registration.reference_code,
            "trip_id": result.registration.trip_id,
            "status": result.registration.status,
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# Plain-HTML pages (Jinja templates) for manually testing the flow above.
# No styling — see app/templates/passenger/. These reuse every helper
# and the shared _execute_registration() function above; no business
# logic is duplicated here.
# ---------------------------------------------------------------------------

from datetime import datetime

@passenger_bp.route("/passenger/", methods=["GET"])
def home():

    windy_conditions = monitoring_service.get_dss_weather_conditions()

    # Water Level + Wave Height: same service/functions the Admin Monitoring
    # page uses, so both sides show the same readings and availability.
    water_level_card = monitoring_service.build_water_level_card(
        monitoring_service.get_latest_water_reading()
    )

    latest_reading = monitoring_service.get_latest_reading()

    # Same Safety Status the Admin dashboard shows (single shared source).
    safety_status, safety_label, _ = monitoring_service.get_current_safety()

    # Environmental Status: weather-model condition from the same Open-Meteo
    # reading shown above. Separate from Safety Status (water level) on purpose.
    environmental_condition = scheduling_service.get_environmental_condition(
        windy_conditions["reading"]
        if windy_conditions["status"] in ("live", "cached", "manual")
        else None
    )

    # Recently updated upcoming trip
    effective_departure = func.coalesce(Trip.new_departure_time, Trip.departure_time)
    current_trip = (
        Trip.query
        .filter(Trip.boat_id.isnot(None))
        .filter(effective_departure >= ph_now())
        .order_by(Trip.updated_at.desc())
        .first()
    )

    current_trip_date = None
    current_trip_time = None
    # Same status the Manifests list and Dashboard show (Unassigned / Full /
    # Expired), not the raw stored value.
    current_trip_status = (
        _display_status(current_trip, _reserved_seats_count(current_trip.id))
        if current_trip else None
    )

    if current_trip and current_trip.departure_time:
        shown_departure = current_trip.new_departure_time or current_trip.departure_time
        current_trip_date = shown_departure.strftime("%B %d, %Y")
        current_trip_time = shown_departure.strftime("%I:%M%p").lstrip("0")

    # Published, non-expired notices only (expiry enforced in the service).
    announcements = announcement_service.get_active_announcements_for_passengers(
        limit=2
    )

    # Reference-code lookup feedback (set by registration_status_lookup).
    # Only known messages are shown, so a crafted URL cannot display
    # arbitrary text.
    lookup_error = request.args.get("lookup_error", "")
    if lookup_error not in LOOKUP_ERRORS:
        lookup_error = None
    # Only the 6 digits are typed into the box; the prefix is fixed text.
    lookup_value = "".join(
        ch for ch in _reference_tail(request.args.get("reference_code", ""))
        if ch in "0123456789"
    )[:6]

    return render_template(
        "passenger/home.html",
        announcements=announcements,
        lookup_error=lookup_error,
        lookup_value=lookup_value,
        reference_prefix=REFERENCE_PREFIX,

        latest_reading=latest_reading,

        water_level_card=water_level_card,

        safety_status=safety_status,
        safety_label=safety_label,
        environmental_condition=environmental_condition,

        windy_status=windy_conditions["status"],
        windy_reading=windy_conditions["reading"],
        windy_message=windy_conditions["message"],

        current_trip=current_trip,
        current_trip_status=current_trip_status,
        current_trip_date=current_trip_date,
        current_trip_time=current_trip_time,

        active_page="home",
    )

@passenger_bp.route("/passenger/announcements", methods=["GET"])
def announcements():
    announcement_rows = announcement_service.get_active_announcements_for_passengers()

    return render_template(
        "passenger/announcements.html",
        announcements=announcement_rows,
        active_page="announcements",
    )

@passenger_bp.route("/passenger/schedule", methods=["GET"])
def schedule():
    trips = _upcoming_trips_query().all()

    trip_rows = [_serialize_trip_summary(t) for t in trips]

    return render_template(
        "passenger/schedule.html",
        trips=trip_rows
    )


@passenger_bp.route("/passenger/trips/<int:trip_id>", methods=["GET"])
def trip_detail_page(trip_id):
    trip = db.session.get(Trip, trip_id)
    if not trip:
        abort(404)
    return render_template("passenger/trip_detail.html", trip=_serialize_trip_detail(trip))

@passenger_bp.route("/passenger/register", methods=["GET"])
def register_page():
    trip_rows = [_serialize_trip_summary(t) for t in _registrable_trips()]
    return render_template(
        "passenger/register.html",
        trips=trip_rows,
        passenger_types=sorted(VALID_PASSENGER_TYPES),
        terminal_hours=TERMINAL_HOURS,
        max_party=MAX_PARTY,
        active_page="register",
    )

@passenger_bp.route("/passenger/register/submit-inline", methods=["POST"])
def submit_registration_inline():

    payload = request.form.to_dict()

    field_errors, cleaned = _validate_payload(payload)

    if field_errors:
        return jsonify({
            "success": False,
            "message": "One or more fields are invalid.",
            "fields": field_errors,
        }), 400

    result = _execute_registration(
        cleaned["trip_id"],
        cleaned
    )

    if not result.success:

        status = (
            404
            if result.error_code == "TRIP_NOT_FOUND"
            else 409
            if result.error_code in (
                "TRIP_FULL",
                "TRIP_NOT_AVAILABLE",
                "DUPLICATE_PASSENGER",
            )
            else 500
        )

        return jsonify({
            "success": False,
            "error_code": result.error_code,
            "message": result.message,
        }), status

    return jsonify({
        "success": True,
        "message": "Registration submitted successfully.",
        "data": {
            "reference_code": result.registration.reference_code,
            "trip_id": result.registration.trip_id,
            "status": result.registration.status,
        },
    }), 201

# ---------------------------------------------------------------------------
# GET /api/passenger/registrations/<reference_code>
# ---------------------------------------------------------------------------


def _find_registrations_by_reference_code(reference_code):
    return (
        PendingRegistration.query
        .filter_by(reference_code=reference_code.strip().upper())
        .order_by(PendingRegistration.id.asc())
        .all()
    )


def _find_registration_by_reference_code(reference_code):
    registrations = _find_registrations_by_reference_code(
        reference_code
    )
    return registrations[0] if registrations else None


def _reopen_trip_if_seats_free(trip):
    """After a cancellation: flush, then free a "Full" trip that now has
    seats (same rule the old cancel route used)."""
    db.session.flush()
    if trip.status == "Full":
        _, _, remaining = _trip_capacity_info(trip)
        if remaining is not None and remaining > 0:
            trip.status = "Delayed" if trip.new_departure_time else "Open"


def _json_error(result):
    body = {
        "success": False,
        "error_code": result.code,
        "message": result.message,
    }
    resp = jsonify(body)
    resp.status_code = result.status
    if result.retry_after:
        resp.headers["Retry-After"] = str(result.retry_after)
    return resp


def _load_public(reference_code):
    parent, passengers = reg_svc.load_registration(reference_code)
    if not parent:
        return None, [], None
    trip = db.session.get(Trip, parent.trip_id)
    return parent, passengers, trip


_NOT_FOUND = ("REGISTRATION_NOT_FOUND", "Registration not found.")


@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>",
    methods=["GET"]
)
def get_registration_status(reference_code):
    parent, passengers, trip = _load_public(reference_code)
    if not parent:
        return error_response(*_NOT_FOUND, status=404)

    return success_response(
        data=reg_svc.serialize_status(
            parent, passengers, trip, _mask_name, _has_departure_time_passed
        )
    )


@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>/passengers",
    methods=["GET"]
)
def get_active_passengers(reference_code):
    parent, passengers, _trip = _load_public(reference_code)
    if not parent:
        return error_response(*_NOT_FOUND, status=404)

    return success_response(data={
        "reference_code": parent.reference_code,
        "active_passengers": reg_svc.serialize_active_passengers(
            passengers, _mask_name
        ),
    })


@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>/cancellation-eligibility",
    methods=["GET"]
)
def get_cancellation_eligibility(reference_code):
    parent, passengers, trip = _load_public(reference_code)
    if not parent:
        return error_response(*_NOT_FOUND, status=404)

    can_cancel, reason = reg_svc.cancellation_eligibility(
        passengers, trip, _has_departure_time_passed
    )
    return success_response(data={
        "reference_code": parent.reference_code,
        "can_cancel": can_cancel,
        "reason": reason,
        "verification_required": True,
    })


@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>/cancellations",
    methods=["GET"]
)
def get_cancellation_history(reference_code):
    parent, passengers, _trip = _load_public(reference_code)
    if not parent:
        return error_response(*_NOT_FOUND, status=404)

    return success_response(
        data=reg_svc.serialize_cancellation_history(
            parent, passengers, _mask_name
        )
    )


LOOKUP_MSG_MISSING = "Please enter your reference code."
LOOKUP_MSG_INVALID = "Please enter the 6 digits from your SMS."
LOOKUP_MSG_NOT_FOUND = "No registration found for that reference code."
LOOKUP_ERRORS = {LOOKUP_MSG_MISSING, LOOKUP_MSG_INVALID, LOOKUP_MSG_NOT_FOUND}

# Every reference code starts with this fixed prefix; the passenger only
# types the 6 digits that follow it. Keep this in step with
# generate_reference_code().
REFERENCE_PREFIX = "WT-2026-"
_REFERENCE_DIGITS_RE = re.compile(r"[0-9]{6}")


def _reference_tail(raw):
    """What the passenger typed, minus spaces and the fixed prefix.

    Accepts the bare digits ("123456") as well as a full code
    ("WT-2026-123456"), so a pasted code still works."""
    text = "".join(str(raw or "").split()).upper()
    if text.startswith(REFERENCE_PREFIX):
        text = text[len(REFERENCE_PREFIX):]
    return text


def _reference_from_lookup_input(raw):
    """Full reference code, or None unless it is exactly 6 digits."""
    tail = _reference_tail(raw)
    if _REFERENCE_DIGITS_RE.fullmatch(tail):
        return REFERENCE_PREFIX + tail
    return None


def _home_with_lookup_error(message, typed=""):
    params = {"lookup_error": message}
    if typed:
        params["reference_code"] = typed
    return redirect(url_for("passenger.home", **params) + "#registration-status")


@passenger_bp.route("/passenger/registrations", methods=["GET"])
def registration_status_lookup():
    """Reference-code lookup submitted from the Passenger Home page.

    Read-only: nothing is created or changed. Validation happens here on
    the server; the form's own checks are only a convenience."""
    raw = request.args.get("reference_code", "")

    if not _reference_tail(raw):
        return _home_with_lookup_error(LOOKUP_MSG_MISSING)

    reference_code = _reference_from_lookup_input(raw)

    if not reference_code:
        return _home_with_lookup_error(
            LOOKUP_MSG_INVALID, _reference_tail(raw)[:30]
        )

    if not _find_registration_by_reference_code(reference_code):
        return _home_with_lookup_error(LOOKUP_MSG_NOT_FOUND, reference_code)

    return redirect(
        url_for(
            "passenger.registration_status_page",
            reference_code=reference_code
        )
    )

REGISTRATION_STATUS_LABELS = {
    "pending": "Pending Approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled by passenger",
    "partial": "Partially Approved",
}

PASSENGER_STATUS_LABELS = {
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Rejected",
}


def _group_status_summary(registrations):
    """Overall status of one reference code, worked out from the passengers
    who are still on it (cancelled passengers are left out).

    state is one of: pending, approved, rejected, partial, cancelled, other.
    "partial" means at least one passenger is approved but not everyone is.
    Also returns the per-passenger rows shown on the status page."""
    rows = []
    for reg in registrations:
        key = (reg.status or "").strip().lower()
        if key == "cancelled":
            continue
        if key not in PASSENGER_STATUS_LABELS:
            key = "other"
        rows.append({
            "name": _mask_name(reg.full_name),
            "status": key,
            "label": PASSENGER_STATUS_LABELS.get(key, "Other"),
        })

    counts = {
        "total": len(rows),
        "approved": sum(1 for p in rows if p["status"] == "approved"),
        "pending": sum(1 for p in rows if p["status"] == "pending"),
        "rejected": sum(1 for p in rows if p["status"] == "rejected"),
    }

    if not rows:
        state = "cancelled"
    elif counts["approved"] == counts["total"]:
        state = "approved"
    elif counts["rejected"] == counts["total"]:
        state = "rejected"
    elif counts["approved"] > 0:
        state = "partial"
    elif counts["pending"] > 0:
        state = "pending"
    else:
        state = "other"

    return state, counts, rows


def _registration_status_label(status):
    """Same wording Step 3 shows; unknown statuses get a tidy fallback."""
    raw = (status or "").strip()
    return REGISTRATION_STATUS_LABELS.get(raw.lower(), raw.replace("_", " ").title())


@passenger_bp.route(
    "/passenger/registrations/<reference_code>",
    methods=["GET"]
)
def registration_status_page(reference_code):

    registrations = _find_registrations_by_reference_code(
        reference_code
    )

    if not registrations:
        return _home_with_lookup_error(
            LOOKUP_MSG_NOT_FOUND,
            reg_svc.normalize_reference(reference_code),
        )

    registration = registrations[0]

    trip = db.session.get(Trip, registration.trip_id)

    left = (
        bool(trip)
        and (
            trip.status == "Departed"
            or _has_departure_time_passed(trip)
        )
    )

    pending_passengers = [
        r for r in registrations if r.status == "pending"
    ]

    group_state, status_counts, passenger_rows = _group_status_summary(
        registrations
    )

    return render_template(
        "passenger/registration_success.html",
        registration=registration,
        registrations=registrations,
        trip=_serialize_trip_detail(trip) if trip else None,
        lookup_error=None,
        status_label=_registration_status_label(group_state),
        masked_name=_mask_name(registration.full_name),
        parent_status=group_state,
        status_counts=status_counts,
        passenger_rows=passenger_rows,
        active_passengers=reg_svc.serialize_active_passengers(
            registrations, _mask_name
        ),
        can_cancel=bool(pending_passengers) and not left,
        is_group=len(registrations) > 1
    )


def _client_ip():
    # Behind a reverse proxy, configure werkzeug ProxyFix so this is the
    # real client address.
    return request.remote_addr


@passenger_bp.route(
    "/passenger/registrations/<reference_code>/cancel",
    methods=["POST"]
)
@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>/cancel",
    methods=["POST"]
)
def cancel_registration(reference_code):
    payload = request.get_json(silent=True) or {}

    ref = reg_svc.normalize_reference(reference_code)
    stored = session.get("cancellation_verification") or {}
    token = payload.get("verification_token") or (
        stored.get("token") if stored.get("reference") == ref else None
    )

    # Explicit type wins; otherwise infer (keeps the existing front end,
    # which sends passenger_ids only for a partial cancellation).
    passenger_ids = payload.get("passenger_ids")
    cancel_type = payload.get("cancel_type")
    if cancel_type is None:
        if passenger_ids is not None:
            cancel_type = "partial"
        else:
            parent, _p = reg_svc.load_registration(ref)
            cancel_type = (
                "solo" if parent and parent.registration_type == "solo" else "full"
            )

    result = reg_svc.cancel_passengers(
        ref, token, cancel_type, passenger_ids,
        payload.get("reason"),
        trip_has_left=lambda t: t.status == "Departed" or _has_departure_time_passed(t),
        reopen_trip=_reopen_trip_if_seats_free,
    )

    if not result.ok:
        return _json_error(result)

    session.pop("cancellation_verification", None)
    d = result.data

    try:
        log_action(
            user_id=None,
            admin_name="System",
            action=ACTION_PASSENGER_CANCELLED,
            details=(
                f"Reference {d['reference_code']}: {d['seats_released']} "
                f"passenger(s) cancelled ({d['cancel_type']}). "
                f"Reason: {result.notify['reason'] or 'No reason given'}"
            ),
        )
        if result.notify["contact_number"]:
            unisms_service.send_sms(
                result.notify["contact_number"],
                (
                    f"Talim Island Ferry: {d['seats_released']} passenger(s) "
                    f"under reference {d['reference_code']} have been "
                    f"cancelled. Remaining passengers: {len(d['remaining'])}."
                ),
            )
    except Exception:
        current_app.logger.warning(
            "Cancellation SMS/logging failed for %s",
            d["reference_code"], exc_info=True,
        )

    mask = lambda rows: [{"id": i, "name": _mask_name(n)} for i, n in rows]
    return jsonify({
        "success": True,
        "message": "Cancellation completed successfully.",
        "data": {
            "reference_code": d["reference_code"],
            "registration_status": d["registration_status"],
            "cancel_type": d["cancel_type"],
            "cancelled_count": len(d["cancelled"]),
            "remaining_count": len(d["remaining"]),
            "cancelled_passengers": mask(d["cancelled"]),
            "remaining_passengers": mask(d["remaining"]),
            "seats_released": d["seats_released"],
        },
    }), 200


@passenger_bp.route("/passenger/guide", methods=["GET"])
def guide():
    return render_template(
        "passenger/guide.html",
        active_page="guide",
    )


@passenger_bp.route(
    "/passenger/registrations/<reference_code>/verify",
    methods=["POST"]
)
@passenger_bp.route(
    "/api/passenger/registrations/<reference_code>/verify",
    methods=["POST"]
)
def verify_registration_identity(reference_code):
    last_four = (
        request.form.get("last_four")
        or (request.get_json(silent=True) or {}).get("last_four")
        or ""
    )

    result = reg_svc.verify_identity(
        reference_code, last_four, _client_ip(),
        trip_has_left=lambda t: t.status == "Departed" or _has_departure_time_passed(t),
    )
    if not result.ok:
        return _json_error(result)

    ref = result.data["reference_code"]
    # Cookie session carries the token for the existing front end; it is
    # bound to this reference and expires server-side regardless.
    session["cancellation_verification"] = {
        "reference": ref,
        "token": result.data["token"],
    }

    parent, passengers, trip = _load_public(ref)
    return jsonify({
        "success": True,
        "message": "Identity verified successfully.",
        "data": {
            "reference_code": ref,
            "verification_token": result.data["token"],
            "expires_in": result.data["expires_in"],
            "registration_type": parent.registration_type,
            "passenger_count": len(passengers),
            "passengers": reg_svc.serialize_active_passengers(
                passengers, _mask_name
            ),
        },
    }), 200