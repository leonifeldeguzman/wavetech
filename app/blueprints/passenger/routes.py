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
from flask import current_app, request, render_template, redirect, url_for, abort, jsonify

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
from app.services import monitoring_service, scheduling_service
from sqlalchemy import func
from app.utils.ph_clock import ph_now
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

def _reserved_seats_count(trip_id):
    """
    Reserved/occupied seats = passengers already on the manifest
    (ManifestEntry, regardless of source) + online registrations still
    awaiting admin decision (PendingRegistration.status == "pending").

    Rejected registrations never count. Approved registrations are NOT
    double-counted here: once approved, the existing admin flow
    (approve_registration in app/blueprints/manifests/routes.py) creates
    a ManifestEntry AND flips the PendingRegistration.status to
    "approved" — it does not delete the pending row. Excluding
    status != "pending" here avoids counting that seat twice.
    """
    from app.models.manifest_entry import ManifestEntry

    manifest_count = ManifestEntry.query.filter_by(trip_id=trip_id).count()
    pending_count = PendingRegistration.query.filter_by(
        trip_id=trip_id, status="pending"
    ).count()
    return manifest_count + pending_count


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
    capacity, reserved, remaining = _trip_capacity_info(trip)
    if remaining is not None and remaining <= 0:
        return False, "TRIP_FULL", "This trip is already full."
    return True, None, None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

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
        "status": trip.status,
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
    def __init__(self, success, error_code=None, message=None, registration=None):
        self.success = success
        self.error_code = error_code
        self.message = message
        self.registration = registration


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

def _execute_registration(trip_id, cleaned):
    """Runs the actual registration transaction: lock trip -> check
    status/capacity -> create PendingRegistration -> commit. Used by
    both /api/passenger/registrations (JSON) and the plain-HTML
    registration form route, so the business logic is defined exactly
    once."""
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

        registration = PendingRegistration(
            trip_id=trip.id,
            full_name=cleaned["full_name"],
            age=cleaned["age"],
            address=cleaned["address"],
            contact_number=cleaned["contact_number"],
            passenger_type=cleaned["passenger_type"],
            status="pending",
        )

        # generate_reference_code() is randomized; retry on the (rare)
        # unique-constraint collision instead of failing the whole
        # registration outright.
        for _ in range(REFERENCE_CODE_MAX_ATTEMPTS):
            registration.reference_code = generate_reference_code()
            try:
                db.session.add(registration)
                db.session.flush()  # surface IntegrityError before commit
                break
            except IntegrityError:
                db.session.rollback()
                # Re-acquire the trip lock after rollback, since rollback
                # released it.
                trip = Trip.query.filter_by(id=trip_id).with_for_update().first()
                continue
        else:
            db.session.rollback()
            return RegistrationResult(
                False,
                "REFERENCE_CODE_GENERATION_FAILED",
                "Could not generate a unique reference code. Please try again.",
            )

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
        if i > 0 and raw.get("same_as_contact"):
            merged["address"] = contact_in.get("address")

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
    """Best-effort: one SMS to the contact person listing every code."""
    try:
        shown = trip.new_departure_time or trip.departure_time
        when = shown.strftime("%b %d, %Y, %I:%M %p") if shown else "TBA"
        codes = ", ".join(r.reference_code for r in registrations)
        label = "Ref" if len(registrations) == 1 else f"Refs ({len(registrations)} passengers)"
        message = (
            f"Talim Island Ferry: Registration confirmed. {label}: {codes}. "
            f"Trip: {trip.route_origin} to {trip.route_destination}, departing {when}."
        )
        result = unisms_service.send_sms(registrations[0].contact_number, message)
        if result.status != "skipped_no_number":
            log_action(
                user_id=None, admin_name="System", action=ACTION_REGISTRATION_SMS_SENT,
                details=f"References {codes}, trip #{trip.id}, status={result.status}"
                        + (f", error={result.error}" if result.error else ""),
            )
    except Exception:
        current_app.logger.warning("Party registration SMS failed", exc_info=True)


def _execute_party_registration(trip_id, people):
    """All-or-nothing: lock trip, check seats for the WHOLE party, create one
    PendingRegistration (and one reference code) per passenger, commit."""
    try:
        trip = Trip.query.filter_by(id=trip_id).with_for_update().first()
        if not trip:
            db.session.rollback()
            return RegistrationResult(False, "TRIP_NOT_FOUND", "No trip found with this ID.")

        ok, code, message = _accepts_registration(trip)
        if not ok:
            db.session.rollback()
            return RegistrationResult(False, code, message)

        _, _, remaining = _trip_capacity_info(trip)
        if remaining is not None and len(people) > remaining:
            db.session.rollback()
            return RegistrationResult(
                False, "TRIP_FULL",
                f"Only {remaining} seat(s) left on this trip. Please remove a passenger or pick another trip.")

        registrations, used = [], set()
        for person in people:
            reg = PendingRegistration(trip_id=trip.id, status="pending", **person)
            for _ in range(REFERENCE_CODE_MAX_ATTEMPTS):
                candidate = generate_reference_code()
                if candidate in used or PendingRegistration.query.filter_by(
                        reference_code=candidate).first():
                    continue
                reg.reference_code = candidate
                break
            else:
                db.session.rollback()
                return RegistrationResult(False, "REFERENCE_CODE_GENERATION_FAILED",
                                          "Could not generate a reference code. Please try again.")
            used.add(reg.reference_code)
            db.session.add(reg)
            registrations.append(reg)

        db.session.flush()
        _, _, remaining = _trip_capacity_info(trip)
        if remaining is not None and remaining <= 0:
            trip.status = "Full"
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Party registration failed")
        return RegistrationResult(False, "REGISTRATION_FAILED",
                                  "Registration could not be completed due to a server error.")

    _send_party_sms(registrations, trip)
    return RegistrationResult(True, registration=registrations)


@passenger_bp.route("/passenger/register/submit-party", methods=["POST"])
def submit_party_registration():
    payload = request.get_json(silent=True) or {}
    errors, trip_id, people = _validate_party(payload)
    if errors:
        return jsonify({"success": False, "message": next(iter(errors.values())),
                        "fields": errors}), 400

    result = _execute_party_registration(trip_id, people)
    if not result.success:
        status = 404 if result.error_code == "TRIP_NOT_FOUND" else (
            409 if result.error_code in ("TRIP_FULL", "TRIP_NOT_AVAILABLE") else 500)
        return jsonify({"success": False, "error_code": result.error_code,
                        "message": result.message}), status

    return jsonify({"success": True, "data": {
        "trip_id": trip_id,
        "registrations": [{"reference_code": r.reference_code, "full_name": r.full_name}
                          for r in result.registration],
    }}), 201


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
            409 if result.error_code in ("TRIP_FULL", "TRIP_NOT_AVAILABLE") else 500
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

    if current_trip and current_trip.departure_time:
        shown_departure = current_trip.new_departure_time or current_trip.departure_time
        current_trip_date = shown_departure.strftime("%B %d, %Y")
        current_trip_time = shown_departure.strftime("%I:%M%p").lstrip("0")

    # Published, non-expired notices only (expiry enforced in the service).
    announcements = announcement_service.get_active_announcements_for_passengers(
        limit=2
    )

    return render_template(
        "passenger/home.html",
        announcements=announcements,

        latest_reading=latest_reading,

        water_level_card=water_level_card,

        safety_status=safety_status,
        safety_label=safety_label,
        environmental_condition=environmental_condition,

        windy_status=windy_conditions["status"],
        windy_reading=windy_conditions["reading"],
        windy_message=windy_conditions["message"],

        current_trip=current_trip,
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

@passenger_bp.route("/passenger/trips/<int:trip_id>/register", methods=["GET"])
def registration_form_page(trip_id):
    trip = db.session.get(Trip, trip_id)
    if not trip:
        abort(404)
    return render_template(
        "passenger/register_form.html",
        trip=_serialize_trip_summary(trip),
        passenger_types=sorted(VALID_PASSENGER_TYPES),
        field_errors={},
        form_values={},
    )


@passenger_bp.route("/passenger/trips/<int:trip_id>/register", methods=["POST"])
def submit_registration_page(trip_id):
    trip = db.session.get(Trip, trip_id)
    if not trip:
        abort(404)

    # request.form is an ImmutableMultiDict; .get() behaves the same way
    # a plain dict's .get() does, so the existing _validate_payload()
    # works unmodified against either JSON or form-encoded input.
    payload = dict(request.form)
    payload.setdefault("trip_id", trip_id)

    field_errors, cleaned = _validate_payload(payload)
    if field_errors:
        return render_template(
            "passenger/register_form.html",
            trip=_serialize_trip_summary(trip),
            passenger_types=sorted(VALID_PASSENGER_TYPES),
            field_errors=field_errors,
            form_values=request.form,
        ), 400

    result = _execute_registration(trip_id, cleaned)

    if not result.success:
        field_errors = {"_general": result.message}
        return render_template(
            "passenger/register_form.html",
            trip=_serialize_trip_summary(trip),
            passenger_types=sorted(VALID_PASSENGER_TYPES),
            field_errors=field_errors,
            form_values=request.form,
        ), 409 if result.error_code in ("TRIP_FULL", "TRIP_NOT_AVAILABLE") else 500

    return redirect(url_for(
        "passenger.registration_status_page",
        reference_code=result.registration.reference_code,
    ))


# ---------------------------------------------------------------------------
# GET /api/passenger/registrations/<reference_code>
# ---------------------------------------------------------------------------

def _find_registration_by_reference_code(reference_code):
    """Shared lookup used by both the JSON status endpoint and the HTML
    status page below, so the query only exists in one place."""
    return PendingRegistration.query.filter_by(reference_code=reference_code).first()


def _serialize_registration_status(registration):
    """Deliberately minimal — only what a passenger needs to confirm
    their registration status. Address/contact_number are left out since
    the passenger already has them; there's no reason to echo them back
    over an endpoint that only requires knowing a reference code."""
    return {
        "reference_code": registration.reference_code,
        "full_name": _mask_name(registration.full_name),
        "trip_id": registration.trip_id,
        "status": registration.status,
    }


@passenger_bp.route("/api/passenger/registrations/<reference_code>", methods=["GET"])
def get_registration_status(reference_code):
    registration = _find_registration_by_reference_code(reference_code)
    if not registration:
        return error_response("REGISTRATION_NOT_FOUND", "Registration not found.", status=404)
    return success_response(data=_serialize_registration_status(registration))


@passenger_bp.route("/passenger/registrations", methods=["GET"])
def registration_status_lookup():
    reference_code = request.args.get("reference_code", "").strip()

    if not reference_code:
        return redirect(
            url_for(
                "passenger.home",
                lookup_error="Please enter a reference code."
            ) + "#registration-status"
        )

    registration = _find_registration_by_reference_code(reference_code)

    if not registration:
        return redirect(
            url_for(
                "passenger.home",
                lookup_error="No registration found for that reference code."
            ) + "#registration-status"
        )

    return redirect(
        url_for(
            "passenger.registration_status_page",
            reference_code=reference_code
        )
    )

REGISTRATION_STATUS_LABELS = {
    "pending": "Pending Verification",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled by passenger",
}


def _registration_status_label(status):
    """Same wording Step 3 shows; unknown statuses get a tidy fallback."""
    raw = (status or "").strip()
    return REGISTRATION_STATUS_LABELS.get(raw.lower(), raw.replace("_", " ").title())

@passenger_bp.route("/passenger/registrations/<reference_code>", methods=["GET"])
def registration_status_page(reference_code):
    registration = _find_registration_by_reference_code(reference_code)
    if not registration:
        return render_template(
            "passenger/registration_success.html",
            registration=None, trip=None, status_label=None, can_cancel=False,
            masked_name=None, masked_phone=None,
            lookup_error="No registration found for that reference code.",
        )

    trip = db.session.get(Trip, registration.trip_id)
    left = bool(trip) and (trip.status == "Departed" or _has_departure_time_passed(trip))
    return render_template(
        "passenger/registration_success.html",
        registration=registration,
        trip=_serialize_trip_detail(trip) if trip else None,
        lookup_error=None,
        status_label=_registration_status_label(registration.status),
        masked_name=_mask_name(registration.full_name),
        masked_phone=_mask_phone(registration.contact_number),
        can_cancel=registration.status in CANCELLABLE_STATUSES and not left,
    )

@passenger_bp.route("/passenger/registrations/<reference_code>/cancel", methods=["POST"])
def cancel_registration(reference_code):
    generic = {"success": False,
               "message": "We couldn't find a matching registration. Check the code and phone number."}
    phone = normalize_ph_mobile(request.form.get("contact_number"))
    reason = (request.form.get("reason") or "").strip()[:200]

    reg = (PendingRegistration.query
           .filter_by(reference_code=reference_code.strip().upper())
           .with_for_update().first())
    # Same message whether the code or the phone is wrong, so codes can't be probed.
    if not reg or not phone or normalize_ph_mobile(reg.contact_number) != phone:
        db.session.rollback()
        return jsonify(generic), 404

    if reg.status == "cancelled":
        return jsonify({"success": True, "message": "Your registration was cancelled."})
    if reg.status not in CANCELLABLE_STATUSES:
        db.session.rollback()
        return jsonify({"success": False,
                        "message": "This registration can no longer be cancelled online. "
                                   "Please see the terminal staff."}), 409

    trip = db.session.get(Trip, reg.trip_id)
    if trip and (trip.status == "Departed" or _has_departure_time_passed(trip)):
        db.session.rollback()
        return jsonify({"success": False,
                        "message": "This trip has already left, so it can't be cancelled."}), 409

    reg.status = "cancelled"            # seat is freed: only "pending" rows count as reserved
    if trip and trip.status == "Full":  # reopen if the seat made room
        _, _, remaining = _trip_capacity_info(trip)
        if remaining and remaining > 0:
            trip.status = "Delayed" if trip.new_departure_time else "Open"
    db.session.commit()

    try:
        log_action(user_id=None, admin_name="System", action=ACTION_PASSENGER_CANCELLED,
                   details=f"Passenger cancelled {reg.reference_code} ({reason or 'no reason given'})")
        unisms_service.send_sms(
            reg.contact_number, f"Your registration {reg.reference_code} has been cancelled.")
    except Exception:
        current_app.logger.warning("Cancel log/SMS failed for %s", reg.reference_code, exc_info=True)

    return jsonify({"success": True, "message": "Your registration was cancelled."})

@passenger_bp.route("/passenger/guide", methods=["GET"])
def guide():
    return render_template(
        "passenger/guide.html",
        active_page="guide",
    )