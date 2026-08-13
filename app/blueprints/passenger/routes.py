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
from flask import request, render_template, redirect, url_for, abort

from sqlalchemy.exc import IntegrityError

from app.blueprints.passenger import passenger_bp
from app.extensions import db
from app.models.trip import Trip
from app.models.pending_registration import PendingRegistration, generate_reference_code
from app.utils.api_responses import success_response, error_response


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


def _accepts_registration(trip):
    """Returns (can_register, error_code, message). error_code/message
    are only meaningful when can_register is False. Returning an
    explicit code here (rather than inferring one from the message text
    later) avoids the two being able to drift out of sync."""
    if trip.status == "Full":
        return False, "TRIP_FULL", "This trip is already full."
    if trip.status in STATUS_BLOCKS_REGISTRATION:
        return False, "TRIP_NOT_AVAILABLE", "This trip is no longer accepting registrations."
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

    return {
        "trip_id": trip.id,
        "departure_time": trip.departure_time.isoformat() if trip.departure_time else None,
        "route_origin": trip.route_origin,
        "route_destination": trip.route_destination,
        "status": trip.status,
        "boat": {"id": trip.boat.id, "name": trip.boat.name} if trip.boat else None,
        "capacity": capacity,
        "current_passenger_count": reserved,
        "remaining_capacity": remaining,
        "accepts_registration": can_register,
        "notice": block_reason,
    }


def _serialize_trip_detail(trip):
    data = _serialize_trip_summary(trip)
    data.update({
        "crew_name": trip.crew_name,
        "delay_reason": trip.delay_reason,
        "cancel_reason": trip.cancel_reason,
        "new_departure_time": trip.new_departure_time.isoformat() if trip.new_departure_time else None,
        "departed_at": trip.departed_at.isoformat() if trip.departed_at else None,
    })
    return data


# ---------------------------------------------------------------------------
# GET /api/trips
# ---------------------------------------------------------------------------

@passenger_bp.route("/api/trips", methods=["GET"])
def list_trips():
    trips = (
        Trip.query.filter(~Trip.status.in_(NON_LISTABLE_STATUSES))
        .order_by(Trip.departure_time)
        .all()
    )
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

def _validate_payload(payload):
    fields = {}

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

    return fields, {
        "trip_id": trip_id if not fields.get("trip_id") else None,
        "full_name": str(full_name).strip() if full_name else None,
        "age": age if not fields.get("age") else None,
        "address": str(address).strip() if address else None,
        "contact_number": str(contact_number).strip() if contact_number else None,
        "passenger_type": passenger_type,
    }


class RegistrationResult:
    """Simple result object returned by _execute_registration(), shared
    between the JSON API route and the HTML form route below so the
    transaction/locking/business logic only exists in one place."""
    def __init__(self, success, error_code=None, message=None, registration=None):
        self.success = success
        self.error_code = error_code
        self.message = message
        self.registration = registration


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

    return RegistrationResult(True, registration=registration)


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

@passenger_bp.route("/passenger/", methods=["GET"])
def home():
    trips = (
        Trip.query.filter(~Trip.status.in_(NON_LISTABLE_STATUSES))
        .order_by(Trip.departure_time)
        .all()
    )
    trip_rows = [_serialize_trip_summary(t) for t in trips]
    return render_template("passenger/schedule.html", trips=trip_rows)


@passenger_bp.route("/passenger/trips/<int:trip_id>", methods=["GET"])
def trip_detail_page(trip_id):
    trip = db.session.get(Trip, trip_id)
    if not trip:
        abort(404)
    return render_template("passenger/trip_detail.html", trip=_serialize_trip_detail(trip))


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
        "full_name": registration.full_name,
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
    """Small convenience redirect so a plain HTML form can look up a
    registration by reference code without needing JavaScript."""
    reference_code = request.args.get("reference_code", "").strip()
    if not reference_code:
        abort(400)
    return redirect(url_for("passenger.registration_status_page", reference_code=reference_code))


@passenger_bp.route("/passenger/registrations/<reference_code>", methods=["GET"])
def registration_status_page(reference_code):
    registration = _find_registration_by_reference_code(reference_code)
    if not registration:
        abort(404)
    trip = db.session.get(Trip, registration.trip_id)
    return render_template(
        "passenger/registration_success.html",
        registration=registration,
        trip=_serialize_trip_detail(trip) if trip else None,
    )