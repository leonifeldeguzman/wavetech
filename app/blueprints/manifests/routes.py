from datetime import date, datetime, time as dt_time, timedelta

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from flask import (
    abort, current_app, flash, jsonify, redirect, render_template,
    request, Response, session, url_for,
)

from app.blueprints.manifests import manifests_bp
from app.blueprints.passenger.routes import validate_passenger_fields
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.models.manifest_entry import ManifestEntry
from app.models.announcement import Announcement
from app.models.pending_registration import PendingRegistration
from app.models.registration import Registration, RegistrationCancellation
from app.models.trip import Trip
from app.services import (
    activity_log_service,
    announcement_service,
    manifest_service,
    pdf_service,
    registration_service,
    scheduling_service,
    unisms_service as sms_service,   # the provider announcement_service already uses
    settings_service,
    timeslot_service,
    trip_state_service,
)
from app.services.announcement_service import AnnouncementValidationError
from app.utils.decorators import login_required
from app.utils.ph_clock import ph_now, ph_today


def _apply_search(query, term, *columns):
    """Case-insensitive, partial-match filter across one or more columns.

    OR's the given columns together so a single search box can match any
    of them (e.g. reference code OR name; name OR passenger type). Uses
    SQLAlchemy's ilike() — parameterized, and portable across SQLite/
    Postgres (it emits LOWER()-based comparisons on backends without a
    native ILIKE) — so this never concatenates user input into SQL.
    Returns the query unchanged when there's no search term, and never
    touches the trip/status scoping already applied by the caller.
    """
    if not term:
        return query
    like = f"%{term}%"
    return query.filter(db.or_(*[column.ilike(like) for column in columns]))

CANCELLED_READ_ONLY_MESSAGE = (
    "This trip was cancelled. Its details, registrations and manifest "
    "are now read-only."
)


def _is_cancelled(trip):
    return trip.status == "Cancelled"


def _read_only_message(trip, now=None):
    """trip_state_service's message for a departed/expired trip, plus the
    same closed-record treatment for a cancelled trip."""
    message = trip_state_service.read_only_message(trip, now=now or ph_now())
    if message:
        return message
    if _is_cancelled(trip):
        return CANCELLED_READ_ONLY_MESSAGE
    return None


def _block_if_read_only(trip):
    """Return a redirect if the trip is a closed record, else None.

    A trip is read-only once it has Departed or its scheduled departure
    (PH date and time) has passed (see trip_state_service). Its trip
    details, registrations and manifest must not change, so every
    modifying route calls this first. Other trips are never affected.
    """
    message = _read_only_message(trip)
    if message:
        flash(message)
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))
    return None


def _is_expired(trip):
    """Same rule as the grey Expired badge on /manifests."""
    return trip_state_service.is_expired(trip, now=ph_now())


# Terminal operating hours for departures (inclusive), Asia/Manila.
TERMINAL_OPEN = dt_time(8, 0)
TERMINAL_CLOSE = dt_time(17, 0)


def _fmt_time(value):
    """8:30 PM style label (no leading zero) used in validation messages."""
    return value.strftime("%I:%M %p").lstrip("0")


PAST_DATE_DELETE_MSG = "Time slots on past dates cannot be deleted."


def _parse_date_arg(value):
    """YYYY-MM-DD -> date, falling back to today (PH) when missing/invalid."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    return ph_today()


def _confirmed_count(trip_id):
    """Confirmed passengers = manifest entries. Pending registrations are NOT
    seats until an admin approves them, so they never count toward capacity
    or the Full status."""
    return ManifestEntry.query.filter_by(trip_id=trip_id).count()


def _confirmed_counts_by_trip(trip_ids):
    """Same count for many trips with one grouped query (timetable)."""
    counts = {trip_id: 0 for trip_id in trip_ids}
    if not trip_ids:
        return counts
    rows = (
        db.session.query(ManifestEntry.trip_id, func.count(ManifestEntry.id))
        .filter(ManifestEntry.trip_id.in_(trip_ids))
        .group_by(ManifestEntry.trip_id)
        .all()
    )
    counts.update(dict(rows))
    return counts


def _render_list(selected_date, add_form=None, add_error=None, status=200):
    """Render the timetable. After a failed Add Time Slot submit, the typed
    values and the error come back in so the modal reopens with them."""
    day_start = datetime.combine(selected_date, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    slots = Trip.query.filter(
        Trip.departure_time >= day_start,
        Trip.departure_time < day_end,
    ).order_by(Trip.departure_time).all()

    expired_ids = {s.id for s in slots if _is_expired(s)}

    # Status shown in the list: "Open" only once boat, operator and crew
    # are all assigned, otherwise "Unassigned" (stored status untouched).
    # "Full" is derived from the confirmed passengers (manifest only) against
    # the boat's capacity, with one grouped query for the whole day.
    seat_counts = _confirmed_counts_by_trip([s.id for s in slots])
    display_status = {
        s.id: manifest_service.effective_status(s, seat_counts[s.id])
        for s in slots
    }

    now = ph_now()
    is_past_date = selected_date < now.date()

    # Slots with passenger records (or already departed) get no trash button,
    # and neither does any slot on a past date (view-only). With no deletable
    # slots the template also hides the checkboxes, Select toggle and bulk bar.
    # The delete routes re-check this on the backend.
    if is_past_date:
        deletable_ids = set()
    else:
        blocked_ids = timeslot_service.undeletable_trip_ids(slots)
        deletable_ids = {s.id for s in slots if s.id not in blocked_ids}

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    return render_template(
        "manifests/list.html",
        slots=slots,
        # Add Time Slot is disabled for a date before today (PH). The
        # browser recomputes this when the date changes; add_slot enforces it.
        is_past_date=is_past_date,
        expired_ids=expired_ids,
        display_status=display_status,
        deletable_ids=deletable_ids,
        selected_date=selected_date,
        prev_date=prev_date,
        next_date=next_date,
        # Add Time Slot modal
        today=now.date(),
        now_iso=now.strftime("%Y-%m-%dT%H:%M:%S"),
        terminal_open=TERMINAL_OPEN.strftime("%H:%M"),
        terminal_close=TERMINAL_CLOSE.strftime("%H:%M"),
        add_form=add_form,
        add_error=add_error,
        # Summary only; details live on the Dashboard (same assessment).
        overall_condition=scheduling_service.get_current_overall_condition(),
        refresh_interval_seconds=settings_service.get_refresh_interval_seconds(),
        active_page="manifests"
    ), status


def _details_assigned(trip):
    """True once operator, crew, boat and capacity are all saved on the trip.
    Registrations, the manifest and departure only open after this."""
    return bool(
        trip.operator_name
        and trip.crew_name
        and trip.boat
        and trip.boat.capacity
    )


def _has_passengers(trip):
    """True when anyone holds a place on this trip: a manifest entry
    (approved online or walk-in) or a registration still waiting for
    approval. Rejected / cancelled registrations do not count."""
    if ManifestEntry.query.filter_by(trip_id=trip.id).first() is not None:
        return True
    return (
        PendingRegistration.query.filter(
            PendingRegistration.trip_id == trip.id,
            PendingRegistration.status.in_(("pending", "approved")),
        ).first()
        is not None
    )


TRIP_INFO_REQUIRED = (
    ("operator_name", "Enter the operator."),
    ("crew_name", "Enter the crew."),
    ("boat_name", "Enter the boat."),
    ("boat_capacity", "Enter the boat\u2019s capacity."),
)

TRIP_INFO_LOCKED_MSG = (
    "This trip already has passengers or registrations, so its trip "
    "details cannot be cleared or left empty."
)


def _require_trip_info(form, errors, trip):
    """Add a 'required' error for every empty Trip Information field.

    Trip information may never be saved empty, so a trip can never fall
    back to Unassigned. When the trip already has passengers or
    registrations the message says why. Errors already found by
    validate_trip_details are kept (first error per field wins).
    Returns the (possibly extended) errors dict.
    """
    locked = _has_passengers(trip)
    for field, message in TRIP_INFO_REQUIRED:
        if field in errors:
            continue
        if not (form.get(field) or "").strip():
            errors[field] = (
                f"{message} {TRIP_INFO_LOCKED_MSG}" if locked else message
            )
    return errors


DETAILS_NOT_ASSIGNED_MSG = (
    "Assign trip details first. Registrations open after trip details "
    "are saved and have been assigned."
)


def _block_if_unassigned(trip):
    """Return a redirect if the trip details are not all assigned, else None."""
    if not _details_assigned(trip):
        flash(DETAILS_NOT_ASSIGNED_MSG)
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))
    return None


# ---------------------------------------------------------------------------
# Delay Trip helpers
# ---------------------------------------------------------------------------

CANCEL_REASONS = (
    "Unsafe water conditions",
    "Boat unavailable",
    "Insufficient passengers",
    "Emergency",
)

DELAY_REASONS = (
    "Weather conditions",
    "Waiting for passengers",
    "Boat maintenance",
    "Coast Guard hold",
)


# "Other" in the reason dropdown: the admin types the reason, which is then
# stored and shown to passengers exactly like a preset reason.
OTHER_REASON = "Other"
REASON_MAX_LENGTH = 100


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _resolve_reason(choice, other_text, presets, field, missing_message):
    """Turn the dropdown choice (+ the typed text for "Other") into the reason.

    Returns (reason, errors). A preset is used as is. For "Other" the typed
    text is the reason (spaces collapsed): required, at most 100 characters.
    ``errors`` is {} when valid, else {field: message} for a missing choice or
    {field + "_other": message} for the typed text.
    """
    choice = (choice or "").strip()
    if choice in presets:
        return choice, {}
    if choice == OTHER_REASON:
        text = " ".join((other_text or "").split())
        if not text:
            return None, {f"{field}_other": "Enter the reason."}
        if len(text) > REASON_MAX_LENGTH:
            return None, {
                f"{field}_other":
                    f"Reason must be {REASON_MAX_LENGTH} characters or fewer."
            }
        return text, {}
    return None, {field: missing_message}


def _parse_clock(value):
    """'HH:MM' (or HH:MM:SS) -> time, else None."""
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime((value or "").strip(), fmt).time()
        except ValueError:
            continue
    return None


def _reason_phrase(reason):
    """'Weather conditions' -> 'weather conditions' (keeps 'Coast Guard').

    A reason typed under "Other" is used as written (only trailing
    punctuation is dropped) so names and acronyms keep their capitals.
    """
    if reason not in DELAY_REASONS and reason not in CANCEL_REASONS:
        return reason.rstrip(" .,;:!")
    if reason.startswith("Coast Guard"):
        return reason
    return reason[:1].lower() + reason[1:]


def _validate_delay(trip, time_str):
    """Server-side rules for the Delay Trip time (the reason is resolved
    separately by _resolve_reason).

    Returns (new_departure_datetime_or_None, {field: message}).
    Order: format, later than current, terminal hours, duplicate time.
    The current trip is excluded from the duplicate check.
    """
    errors = {}
    new_dt = None

    clock = _parse_clock(time_str)
    if clock is None:
        errors["new_departure_time"] = "Please choose a valid departure time."
    else:
        clock = clock.replace(second=0, microsecond=0)
        new_dt = datetime.combine(trip.departure_time.date(), clock)
        current = trip.departure_time.replace(second=0, microsecond=0)
        if new_dt <= current:
            errors["new_departure_time"] = (
                f"New time must be later than {_fmt_time(current)}."
            )
        elif not (TERMINAL_OPEN <= clock <= TERMINAL_CLOSE):
            errors["new_departure_time"] = "New time is outside terminal hours."
        else:
            clash = Trip.query.filter(
                Trip.id != trip.id,
                Trip.departure_time >= new_dt,
                Trip.departure_time < new_dt + timedelta(minutes=1),
            ).first()
            if clash is not None:
                errors["new_departure_time"] = (
                    f"Another trip is already scheduled at {_fmt_time(new_dt)}. "
                    "Choose a different time"
                )

    return new_dt, errors


def _trip_audience(trip):
    """Everyone holding a seat on this trip, counted once.

    Returns (count, contact_numbers, pending_regs_to_cancel):
      * count    - manifest entries (approved online + walk-in) plus
                   registrations still waiting for approval. An approved
                   registration already has a manifest entry, so it is not
                   counted twice.
      * contacts - [(number, passengers)] one SMS per distinct number; a
                   number shared by a group counts all of its passengers.
      * regs     - PendingRegistration rows still active (pending/approved).
    """
    entries = ManifestEntry.query.filter_by(trip_id=trip.id).all()
    regs = PendingRegistration.query.filter(
        PendingRegistration.trip_id == trip.id,
        PendingRegistration.status.in_(("pending", "approved")),
    ).all()
    waiting = [p for p in regs if p.status == "pending"]

    per_number = {}
    for person in list(entries) + waiting:
        number = (person.contact_number or "").strip()
        if number:
            per_number[number] = per_number.get(number, 0) + 1
    contacts = list(per_number.items())      # [(number, passengers), ...]
    return len(entries) + len(waiting), contacts, regs


def _conditions_unavailable():
    """True when the overall condition is "Unavailable".

    Same source as the timetable's overall-condition indicator
    (scheduling_service.get_current_overall_condition), so the Confirm
    Departure warning and that indicator always agree. The service already
    maps anything unknown or failed to Unavailable; a failure here is
    treated the same way, because the operator must then verify by hand.
    """
    try:
        condition = scheduling_service.get_current_overall_condition()
    except Exception:
        current_app.logger.exception("Overall condition lookup failed")
        return True
    return (condition or {}).get("status") == scheduling_service.STATUS_UNAVAILABLE


def _conditions_unsafe():
    """True when the overall condition is "Unsafe" right now.

    Same source as the timetable indicator and the Confirm Departure
    warning. If the lookup fails the state is unknown, so this returns False
    (the Unavailable warning already covers the unknown case)."""
    try:
        condition = scheduling_service.get_current_overall_condition()
    except Exception:
        current_app.logger.exception("Overall condition lookup failed")
        return False
    return (condition or {}).get("status") == scheduling_service.STATUS_UNSAFE


def _notice_choices(form):
    """Which channels the admin ticked: (announcement, sms).

    The two are independent: publish in the system, text the passengers,
    or both."""
    return (
        form.get("notify_announcement") == "yes",
        form.get("notify_sms") == "yes",
    )


def _notify_passengers(trip, contacts, message, *, notice_type, title, expires_at,
                       send_announcement=True, send_sms=True):
    """Publish the trip notice and/or text the trip's passengers.

    ``send_announcement`` and ``send_sms`` pick the channels; a channel that
    is switched off is skipped entirely.

    Returns (notified, announced):
      * announced - True when the notice was published through the existing
        announcement_service (shown on the passenger Notices page). The
        service's own SMS broadcast is switched off: it texts every approved
        passenger on every live trip, which is wrong for one trip's delay or
        cancellation.
      * notified  - passengers whose number got the SMS, sent here through
        the same unisms_service the notice system uses. A number shared by
        a group counts all of its passengers; passengers without a number
        are not counted.
    The notice and the SMS carry the identical ``message``. Neither can
    raise, and a failure of one does not stop the other.
    """
    announced = False
    try:
        for expiry in (expires_at, None) if send_announcement else ():  # no expiry rather than no notice
            try:
                announcement_service.create_announcement(
                    title=title,
                    content=message,
                    type_=notice_type,
                    publish_now=True,
                    expires_at_raw=expiry,
                    created_by_user_id=session.get("user_id"),
                    trip_id=trip.id,
                    send_sms=False,
                )
                announced = True
                break
            except AnnouncementValidationError:
                continue
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Trip notice failed for trip %s", trip.id)

    notified = 0
    for number, passengers in (contacts if send_sms else ()):
        try:
            result = sms_service.send_sms(number, message)
        except Exception:
            current_app.logger.exception("SMS failed for trip %s", trip.id)
            continue
        if result.success:
            notified += passengers
    return notified, announced


def _notice_warning(notified, announced, *, send_announcement=True, send_sms=True):
    """Warning text for a partly failed notification, or None.

    Only the channels that were actually requested can fail."""
    problems = []
    if send_announcement and not announced:
        problems.append("the announcement could not be published")
    if send_sms and not notified:
        problems.append("no SMS could be sent")
    if not problems:
        return None
    return "The trip was updated, but " + " and ".join(problems) + "."


def _delay_response(ok, trip, errors=None, status=200):
    """JSON for the modal (AJAX), or a flash + redirect without JavaScript."""
    target = url_for("manifests.trip_detail", trip_id=trip.id)
    if _is_ajax():
        return jsonify({"ok": ok, "errors": errors or {}, "redirect": target}), status
    if errors:
        flash(next(iter(errors.values())))
    return redirect(target)


@manifests_bp.route("/manifests")
@login_required
def list_slots():
    selected_date = _parse_date_arg(request.args.get("date"))
    return _render_list(selected_date)


def _validate_new_slot(date_str, time_str):
    """Validate an Add Time Slot submission.

    Returns (departure_datetime, None) when valid, else (None, message).
    Checks run in order of relevance: format, past date, terminal hours,
    past time, then duplicates. All comparisons use PH time.
    """
    try:
        slot_date = datetime.strptime(date_str or "", "%Y-%m-%d").date()
    except ValueError:
        return None, "Please choose a valid date."

    slot_time = None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            slot_time = datetime.strptime(time_str or "", fmt).time()
            break
        except ValueError:
            continue
    if slot_time is None:
        return None, "Please choose a valid departure time."

    now = ph_now()

    if slot_date < now.date():
        return None, "That date has already passed."

    if not (TERMINAL_OPEN <= slot_time <= TERMINAL_CLOSE):
        return None, "That time is outside terminal hours (8:00 AM to 5:00 PM)."

    departure = datetime.combine(slot_date, slot_time.replace(second=0))

    if departure < now.replace(second=0, microsecond=0):
        return None, "That time has already passed."

    # A time slot IS a Trip row, so any trip at the same departure is a
    # duplicate. If that trip is still unassigned, say so; otherwise it is
    # a duplicate trip.
    existing = Trip.query.filter(Trip.departure_time == departure).first()
    if existing is not None:
        label = _fmt_time(departure)
        if manifest_service.effective_status(existing) == "Unassigned":
            return None, f"An unassigned slot at {label} already exists."
        return None, f"A trip at {label} already exists for this date."

    return departure, None


@manifests_bp.route("/manifests/add-slot", methods=["POST"])
@login_required
def add_slot():
    slot_date = (request.form.get("slot_date") or "").strip()
    slot_time = (request.form.get("departure_time") or "").strip()
    view_date = _parse_date_arg(request.form.get("view_date"))

    departure, error = _validate_new_slot(slot_date, slot_time)

    # Validation failed: keep the modal open with the admin's input.
    if error:
        return _render_list(
            view_date,
            add_form={"slot_date": slot_date, "departure_time": slot_time},
            add_error=error,
            status=400,
        )

    try:
        new_trip = Trip(departure_time=departure, status="Open")
        db.session.add(new_trip)
        # Staged in the same transaction so the slot and its log row
        # succeed or fail together.
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_TIME_SLOT_CREATED,
            details=activity_log_service.describe_trip(new_trip),
            commit=False,
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return _render_list(
            view_date,
            add_form={"slot_date": slot_date, "departure_time": slot_time},
            add_error="Could not add the time slot. Please try again.",
            status=500,
        )

    flash("Time slot added.", "success")
    return redirect(url_for(
        "manifests.list_slots", date=departure.date().isoformat()
    ))


@manifests_bp.route("/manifests/<int:trip_id>/delete-slot", methods=["POST"])
@login_required
def delete_slot(trip_id):
    """Delete a time slot that has no registered passengers.

    The passenger check runs here under a row lock on the trip (the
    registration flow locks the same row), so hiding the button in the
    template is a convenience only. Passenger and trip records are never
    touched when the check fails.
    """
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first()
    if trip is None:
        flash("That time slot no longer exists.", "warning")
        return redirect(url_for("manifests.list_slots"))

    back = url_for(
        "manifests.list_slots", date=trip.departure_time.date().isoformat()
    )

    if trip.departure_time.date() < ph_today():
        db.session.rollback()  # release the row lock, nothing was changed
        flash(PAST_DATE_DELETE_MSG, "danger")
        return redirect(back)

    reason = timeslot_service.deletion_block_reason(trip)
    if reason:
        db.session.rollback()  # release the row lock, nothing was changed
        flash(reason, "danger")
        return redirect(back)

    try:
        timeslot_service.delete_time_slot(
            trip,
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
        )
    except SQLAlchemyError:
        flash("Could not delete the time slot. Please try again.", "danger")
        return redirect(back)

    flash("Time slot deleted.", "success")
    return redirect(back)


@manifests_bp.route("/manifests/delete-slots", methods=["POST"])
@login_required
def bulk_delete_slots():
    """Delete several time slots in one go (same rules as delete_slot).

    Every slot goes through the same row lock and the same
    timeslot_service.deletion_block_reason() check as the single delete, so a
    slot that gained passengers (or departed) since the page loaded is skipped
    and reported, never deleted. Slots are handled one at a time: one failing
    does not undo the ones already deleted.
    """
    back_date = _parse_date_arg(request.form.get("view_date"))
    back = url_for("manifests.list_slots", date=back_date.isoformat())

    trip_ids = []
    for raw in request.form.getlist("trip_ids"):
        if raw.isdigit() and int(raw) not in trip_ids:
            trip_ids.append(int(raw))
    if not trip_ids:
        flash("Select at least one time slot to delete.", "warning")
        return redirect(back)

    deleted = 0
    failed = 0
    blocked_reasons = []
    blocked_count = 0

    for trip_id in trip_ids:
        trip = Trip.query.filter_by(id=trip_id).with_for_update().first()
        if trip is None:
            continue    # already gone

        if trip.departure_time.date() < ph_today():
            reason = PAST_DATE_DELETE_MSG
        else:
            reason = timeslot_service.deletion_block_reason(trip)
        if reason:
            db.session.rollback()   # release the row lock, nothing changed
            blocked_count += 1
            if reason not in blocked_reasons:
                blocked_reasons.append(reason)
            continue

        try:
            timeslot_service.delete_time_slot(
                trip,
                user_id=session.get("user_id"),
                admin_name=session.get("full_name", "Unknown"),
            )
            deleted += 1
        except Exception:
            # delete_time_slot() rolls back and re-raises any error. Keep
            # going so the slots already deleted are reported, not lost
            # behind a 500 page.
            db.session.rollback()
            current_app.logger.exception(
                "Bulk delete failed for time slot %s", trip_id
            )
            failed += 1

    if deleted:
        flash(
            f"{deleted} time slot{'' if deleted == 1 else 's'} deleted.",
            "success",
        )
    if blocked_count:
        flash(
            f"{blocked_count} time slot{'' if blocked_count == 1 else 's'} "
            "not deleted. " + " ".join(blocked_reasons),
            "danger",
        )
    if failed:
        flash(
            f"{failed} time slot{'' if failed == 1 else 's'} could not be "
            "deleted. Please try again.",
            "danger",
        )
    return redirect(back)


def _taken_times(trip):
    """{'HH:MM': '9:15 AM'} of the OTHER trips on this trip's date.

    Only used for instant feedback in the Delay modal; the route re-checks.
    """
    day_start = datetime.combine(trip.departure_time.date(), datetime.min.time())
    others = Trip.query.filter(
        Trip.id != trip.id,
        Trip.departure_time >= day_start,
        Trip.departure_time < day_start + timedelta(days=1),
    ).all()
    return {t.departure_time.strftime("%H:%M"): _fmt_time(t.departure_time) for t in others}


@manifests_bp.route("/manifests/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    return _render_trip_detail(trip)


def _render_trip_detail(trip, form_values=None, field_errors=None, status=200,
                        walkin_form=None, walkin_error=None,
                        walkin_rows=None, walkin_mode=None):
    """Render the trip page. After a failed Trip Information save, the
    submitted values and per-field errors come back in so nothing the admin
    typed is lost."""

    # Search terms are independent per section and travel via query
    # params only — they never change which trip/rows are in scope,
    # they just add an extra .filter() on top of the SAME queries that
    # were already authorized/scoped to this trip.
    pending_search = (request.args.get("pending_q") or "").strip()
    manifest_search = (request.args.get("manifest_q") or "").strip()

    pending_query = PendingRegistration.query.filter_by(
        trip_id=trip.id, status="pending"
    )
    pending_query = _apply_search(
        pending_query,
        pending_search,
        PendingRegistration.reference_code,
        PendingRegistration.full_name,
    )
    pending = pending_query.order_by(PendingRegistration.submitted_at).all()

    # Capacity/occupancy must always reflect the TRUE manifest, never the
    # searched/filtered subset, so this is counted separately from the
    # (possibly filtered) `manifest` list below.
    current_count = _confirmed_count(trip.id)

    manifest_query = ManifestEntry.query.filter_by(trip_id=trip.id)
    manifest_query = _apply_search(
        manifest_query,
        manifest_search,
        ManifestEntry.full_name,
        ManifestEntry.passenger_type,
    )
    manifest = manifest_query.order_by(ManifestEntry.check_in_time).all()

    capacity = trip.boat.capacity if trip.boat else None

    # Seats taken = confirmed passengers only (pending registrations do not
    # hold a seat), the same count that drives the timetable's Full status
    # and the passenger portal.
    occupied_count = current_count
    capacity_full = manifest_service.is_at_capacity(capacity, occupied_count)

    saved_values = {
        "operator_name": trip.operator_name or "",
        "crew_name": trip.crew_name or "",
        "boat_name": trip.boat.name if trip.boat else "",
        "boat_capacity": str(capacity) if capacity else "",
    }
    values = form_values or dict(saved_values)
    now = ph_now()
    read_only = (
        trip_state_service.is_read_only(trip, now=now) or _is_cancelled(trip)
    )

    return render_template(
        "manifests/detail.html",
        trip=trip,
        pending=pending,
        manifest=manifest,
        suggestions=manifest_service.get_suggestions(),
        values=values,
        field_errors=field_errors or {},
        current_count=current_count,
        capacity=capacity,
        occupied_count=occupied_count,
        capacity_full=capacity_full,
        walkin_form=walkin_form or {},
        walkin_error=walkin_error,
        walkin_rows=walkin_rows,
        walkin_mode=walkin_mode,
        # Names/identity of the manifest rows, for the instant duplicate check
        # in the Add modal (the server re-checks on submit).
        manifest_identities=[
            {
                "name": manifest_service.normalize_name(e.full_name),
                "age": e.age,
                "contact": manifest_service._contact_key(e.contact_number),
            }
            for e in ManifestEntry.query.filter_by(trip_id=trip.id).all()
        ],
        is_expired=trip_state_service.is_expired(trip, now=now),
        # Departed or expired: a closed record. The template hides every
        # editing/management control; the routes refuse changes as well.
        read_only=read_only,
        read_only_message=_read_only_message(trip, now=now),
        summary_details_url=trip_state_service.summary_details_url(trip),
        # Saved trip information, used by the Back button to tell whether
        # the form holds unsaved changes.
        saved_values=saved_values,
        # Weather assessment only makes sense for today's trips (PH date)
        # and never for a closed (departed/expired) trip.
        # It also needs the boat, operator and crew saved: the assessment
        # route refuses an Unassigned trip.
        can_assess=(
            not read_only
            and trip.departure_time.date() == ph_today()
            and _details_assigned(trip)
        ),
        assess_note=(
            "Assign trip details first"
            if (
                not read_only
                and trip.departure_time.date() == ph_today()
                and not _details_assigned(trip)
            )
            else "Available on the trip date only"
        ),
        details_assigned=_details_assigned(trip),
        # Same source as the timetable's overall-condition indicator
        conditions_unavailable=_conditions_unavailable(),
        # Unsafe: the Depart button asks for confirmation in a modal first.
        conditions_unsafe=_conditions_unsafe(),
        has_passengers=_has_passengers(trip),
        # Delay Trip modal
        terminal_open=TERMINAL_OPEN.strftime("%H:%M"),
        terminal_close=TERMINAL_CLOSE.strftime("%H:%M"),
        taken_times=_taken_times(trip),
        # Delay / Cancel modals
        notice_count=_trip_audience(trip)[0],
        cancel_reasons=CANCEL_REASONS,
        display_status=manifest_service.effective_status(trip, occupied_count),
        pending_search=pending_search,
        manifest_search=manifest_search,
        active_page="manifests"
    ), status


@manifests_bp.route("/manifests/<int:trip_id>/assign-boat", methods=["POST"])
@login_required
def assign_boat(trip_id):
    """Save the Trip Information form: operator, crew, boat and capacity.

    Operator / crew / boat are free text (the UI suggests previously saved
    values). Capacity is typed by hand and validated here on the server: it
    must be a positive whole number and may not be lower than the number of
    passengers already on the manifest (overloading). On any error nothing is
    saved and the form is shown again with the problem next to the field.
    """
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked

    current_count = _confirmed_count(trip.id)
    values, errors = manifest_service.validate_trip_details(
        request.form, current_count
    )
    # Operator, crew, boat and capacity are all required. This also stops a
    # trip that has passengers/registrations from going back to Unassigned.
    errors = _require_trip_info(request.form, dict(errors or {}), trip)

    if errors:
        flash("Could not save trip details. Please fix the highlighted fields.")
        return _render_trip_detail(
            trip, form_values=values, field_errors=errors, status=400
        )

    trip.operator_name = values["operator_name"]
    trip.crew_name = values["crew_name"]

    trip.boat = manifest_service.find_or_create_boat(
        values["boat_name"], values["_capacity_int"]
    )

    manifest_service.sync_trip_status(trip, current_count)
    db.session.commit()

    flash("Trip details saved.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

@manifests_bp.route("/manifests/<int:trip_id>/clear-trip-info", methods=["POST"])
@login_required
def clear_trip_info(trip_id):
    """Clear operator, crew, boat and capacity, returning the trip to Unassigned.

    Only allowed while nobody holds a place on the trip (no manifest entry and
    no pending/approved registration), the same rule that stops Save Changes
    from leaving the details empty."""
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked

    if _has_passengers(trip):
        flash(TRIP_INFO_LOCKED_MSG)
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    trip.operator_name = None
    trip.crew_name = None
    trip.boat = None
    db.session.commit()

    flash("Trip details cleared. This trip is unassigned.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


def _missing_walkin_fields(values):
    """Walk-in passengers must have a full name, age and address.
    Returns e.g. "age and address" for what is missing, or "" if complete."""
    missing = [
        label
        for key, label in (
            ("full_name", "full name"),
            ("age", "age"),
            ("address", "address"),
        )
        if not (values.get(key) or "").strip()
    ]
    if len(missing) > 1:
        return ", ".join(missing[:-1]) + " and " + missing[-1]
    return missing[0] if missing else ""


@manifests_bp.route("/manifests/<int:trip_id>/add-walkin", methods=["POST"])
@login_required
def add_walkin(trip_id):
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked

    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned

    # Capacity first (confirmed passengers only, same rule as the Full status
    # and the disabled Add button). Checked under the trip's row lock, so two
    # simultaneous submissions cannot both take the last seat.
    confirmed = _confirmed_count(trip.id)
    if manifest_service.is_at_capacity(trip.boat.capacity, confirmed):
        manifest_service.sync_trip_status(trip, confirmed)
        db.session.commit()
        flash("This trip is at full capacity. No additional passengers can be registered.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    form = {
        key: request.form.get(key, "")
        for key in ("full_name", "age", "address", "contact_number", "passenger_type")
    }
    missing = _missing_walkin_fields(form)
    field_errors, cleaned = validate_passenger_fields(request.form)
    error = None
    if missing:
        error = f"Please enter the {missing}."
    elif field_errors:
        field, message = next(iter(field_errors.items()))
        error = f"{field.replace('_', ' ').capitalize()}: {message}"
    else:
        # Same lock, so a simultaneous duplicate submission cannot slip in.
        error = manifest_service.find_duplicate_message(
            trip.id,
            cleaned["full_name"],
            age=cleaned["age"],
            contact_number=cleaned["contact_number"],
        )
    if error:
        db.session.rollback()   # nothing was changed; release the row lock
        return _render_trip_detail(
            trip, walkin_form=form, walkin_error=error, status=400
        )

    new_entry = ManifestEntry(
        trip_id=trip.id,
        full_name=manifest_service.clean_name(cleaned["full_name"]),
        age=cleaned["age"],
        address=cleaned["address"],
        contact_number=cleaned["contact_number"],
        passenger_type=cleaned["passenger_type"],
        source="walkin"
    )
    db.session.add(new_entry)
    db.session.flush()
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_MANIFEST_CREATED,
        details=(
            "Created a new manifest for a scheduled trip. "
            + activity_log_service.describe_trip(trip)
            + f" | Passenger: {new_entry.full_name}"
        ),
    )

    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

@manifests_bp.route("/manifests/<int:trip_id>/add-walkins-bulk", methods=["POST"])
@login_required
def add_walkins_bulk(trip_id):
    """Register several walk-in passengers at once. All or nothing: if any
    row is invalid, a duplicate, or there are not enough seats, no one is
    saved and the rows come back to the modal with the problems listed."""
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked

    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned

    confirmed = _confirmed_count(trip.id)
    if manifest_service.is_at_capacity(trip.boat.capacity, confirmed):
        manifest_service.sync_trip_status(trip, confirmed)
        db.session.commit()
        flash("This trip is at full capacity. No additional passengers can be registered.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    fields = ("full_name", "age", "address", "contact_number", "passenger_type")
    columns = {f: request.form.getlist(f"bulk_{f}") for f in fields}
    total = max(len(values) for values in columns.values())

    rows = []
    for i in range(total):
        row = {
            f: (columns[f][i] if i < len(columns[f]) else "").strip()
            for f in fields
        }
        # Skip rows the admin left empty (the type always has a default).
        if any(row[f] for f in ("full_name", "age", "address", "contact_number")):
            rows.append(row)

    def _reject(message):
        db.session.rollback()   # nothing was changed; release the row lock
        return _render_trip_detail(
            trip, walkin_rows=rows, walkin_error=message,
            walkin_mode="bulk", status=400,
        )

    if not rows:
        return _reject("Add at least one passenger.")

    seats_left = trip.boat.capacity - confirmed
    if len(rows) > seats_left:
        return _reject(
            f"Only {seats_left} seat{'' if seats_left == 1 else 's'} left, "
            f"but {len(rows)} passengers were entered. "
            f"Remove {len(rows) - seats_left} to continue."
        )

    problems = []
    cleaned_rows = []
    first_seen = {}   # normalized name -> row number, for repeats in this list
    for number, row in enumerate(rows, start=1):
        missing = _missing_walkin_fields(row)
        if missing:
            problems.append(f"Passenger {number}: Please enter the {missing}.")
            continue

        field_errors, cleaned = validate_passenger_fields(row)
        if field_errors:
            field, message = next(iter(field_errors.items()))
            problems.append(
                f"Passenger {number}: {field.replace('_', ' ').capitalize()}: {message}"
            )
            continue

        duplicate = manifest_service.find_duplicate_message(
            trip.id,
            cleaned["full_name"],
            age=cleaned["age"],
            contact_number=cleaned["contact_number"],
        )
        if duplicate:
            problems.append(f"Passenger {number}: {duplicate}")
            continue

        key = " ".join(cleaned["full_name"].split()).lower()
        if key in first_seen:
            problems.append(
                f"Passenger {number}: same name as passenger {first_seen[key]}."
            )
            continue
        first_seen[key] = number
        cleaned_rows.append(cleaned)

    if problems:
        return _reject("\n".join(problems))

    new_entries = []
    for cleaned in cleaned_rows:
        entry = ManifestEntry(
            trip_id=trip.id,
            full_name=manifest_service.clean_name(cleaned["full_name"]),
            age=cleaned["age"],
            address=cleaned["address"],
            contact_number=cleaned["contact_number"],
            passenger_type=cleaned["passenger_type"],
            source="walkin",
        )
        db.session.add(entry)
        new_entries.append(entry)

    db.session.flush()
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    for entry in new_entries:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_MANIFEST_CREATED,
            details=(
                "Created a new manifest for a scheduled trip. "
                + activity_log_service.describe_trip(trip)
                + f" | Passenger: {entry.full_name}"
            ),
        )

    count = len(new_entries)
    flash(
        f"{count} walk-in passenger{'' if count == 1 else 's'} added to the manifest.",
        "success",
    )
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/manifest/<int:entry_id>/edit", methods=["POST"])
@login_required
def edit_manifest_entry(trip_id, entry_id):
    """Admin/Operator edit of an existing (already-approved) manifest
    entry. Only the passenger's own details are editable — id, trip_id,
    source, and check_in_time are preserved exactly as they are, and no
    new row is ever created here.
    """
    trip = Trip.query.get_or_404(trip_id)
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    entry = ManifestEntry.query.get_or_404(entry_id)
    if entry.trip_id != trip.id:
        # Guards against editing an entry that belongs to a different
        # trip via a mismatched/tampered URL.
        abort(404)

    field_errors, cleaned = validate_passenger_fields(request.form)
    if field_errors:
        flash(
            "Could not update passenger — "
            + "; ".join(f"{field}: {message}" for field, message in field_errors.items())
        )
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    duplicate = manifest_service.find_duplicate_message(
        trip.id,
        cleaned["full_name"],
        age=cleaned["age"],
        contact_number=cleaned["contact_number"],
        exclude_entry_id=entry.id,
    )
    if duplicate:
        flash(f"Could not update passenger \u2014 {duplicate}")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    entry.full_name = manifest_service.clean_name(cleaned["full_name"])
    entry.age = cleaned["age"]
    entry.address = cleaned["address"]
    entry.contact_number = cleaned["contact_number"]
    entry.passenger_type = cleaned["passenger_type"]
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_MANIFEST_EDITED,
        details=f"Edited manifest entry for {entry.full_name} on trip #{trip.id}.",
    )

    flash(f"{entry.full_name}'s manifest entry was updated.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/manifest/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_manifest_entry(trip_id, entry_id):
    """Admin/Operator removal of a single manifest entry. Deletes only
    the targeted row — the trip and every other passenger's entry are
    untouched, and the manifest count/capacity shown on the page is
    always recomputed fresh from the database, so it reflects the
    deletion immediately with no separate bookkeeping needed here.
    """
    trip = Trip.query.get_or_404(trip_id)
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    entry = ManifestEntry.query.get_or_404(entry_id)
    if entry.trip_id != trip.id:
        abort(404)

    full_name = entry.full_name
    # Captured before the delete so the log keeps it after the row is gone.
    trip_summary = activity_log_service.describe_trip(trip)
    db.session.delete(entry)
    db.session.flush()
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))   # frees a stored "Full"
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_MANIFEST_DELETED,
        details=(
            "Deleted an existing manifest. "
            + trip_summary
            + f" | Passenger: {full_name}"
        ),
    )

    flash(f"{full_name}'s manifest entry was deleted.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/delay", methods=["POST"])
@login_required
def delay_trip(trip_id):
    # Row lock: two quick submits cannot both pass validation and both notify.
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()

    blocked = _block_if_read_only(trip)
    if blocked:
        if _is_ajax():
            return jsonify({"ok": False, "errors": {}, "redirect": blocked.location}), 409
        return blocked

    if trip.status == "Cancelled":
        return _delay_response(
            False, trip,
            {"form": "This trip is cancelled and cannot be delayed."}, 409,
        )

    new_dt, errors = _validate_delay(trip, request.form.get("new_departure_time"))
    reason, reason_errors = _resolve_reason(
        request.form.get("delay_reason"),
        request.form.get("delay_reason_other"),
        DELAY_REASONS,
        "delay_reason",
        "Select a reason for the delay.",
    )
    errors.update(reason_errors)
    if errors:
        return _delay_response(False, trip, errors, 400)

    original = trip.departure_time
    notice_count, contacts, _regs = _trip_audience(trip)
    send_announcement, send_sms = _notice_choices(request.form)
    wants_notice = (send_announcement or send_sms) and notice_count > 0

    try:
        # The timetable, expiry rule and duplicate checks all read
        # departure_time, so the delayed time must live there. The original
        # time is kept in the Activity Log and the passenger message.
        trip.departure_time = new_dt
        trip.new_departure_time = new_dt
        trip.delay_reason = reason
        trip.status = "Delayed"
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return _delay_response(
            False, trip,
            {"form": "Could not delay the trip. Please try again."}, 500,
        )

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_TRIP_DELAYED,
        details=(
            "Delayed a scheduled trip. "
            + activity_log_service.describe_trip(trip)
            + f" | Original departure: {_fmt_time(original)}"
            + f" | New departure: {_fmt_time(new_dt)}"
            + f" | Reason: {reason}"
        ),
    )

    flash(f"Trip delayed to {_fmt_time(new_dt)}.", "success")

    # Only after the database update has succeeded.
    if wants_notice:
        message = (
            f"Your {_fmt_time(original)} trip is delayed to "
            f"{_fmt_time(new_dt)} due to {_reason_phrase(reason)}."
        )
        notified, announced = _notify_passengers(
            trip, contacts, message,
            notice_type=Announcement.TYPE_DELAY,
            title="Trip delayed",
            expires_at=new_dt.strftime("%Y-%m-%dT%H:%M"),
            send_announcement=send_announcement,
            send_sms=send_sms,
        )
        warning = _notice_warning(
            notified, announced,
            send_announcement=send_announcement, send_sms=send_sms,
        )
        if warning:
            flash(warning, "warning")

    return _delay_response(True, trip)


@manifests_bp.route("/manifests/<int:trip_id>/cancel", methods=["POST"])
@login_required
def cancel_trip(trip_id):
    # Row lock: a double click cannot cancel (or notify) twice.
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()

    blocked = _block_if_read_only(trip)
    if blocked:
        if _is_ajax():
            return jsonify({"ok": False, "errors": {}, "redirect": blocked.location}), 409
        return blocked

    if trip.status == "Cancelled":
        return _delay_response(
            False, trip, {"form": "This trip is already cancelled."}, 409
        )

    reason, reason_errors = _resolve_reason(
        request.form.get("cancel_reason"),
        request.form.get("cancel_reason_other"),
        CANCEL_REASONS,
        "cancel_reason",
        "Select a reason for the cancellation.",
    )
    if reason_errors:
        return _delay_response(False, trip, reason_errors, 400)

    # Taken before anything changes. Only THIS trip's passengers.
    passenger_count, contacts, active_regs = _trip_audience(trip)
    send_announcement, send_sms = _notice_choices(request.form)
    wants_notice = (send_announcement or send_sms) and passenger_count > 0
    departure_label = _fmt_time(trip.departure_time)
    now = ph_now()

    try:
        trip.status = "Cancelled"
        trip.cancel_reason = reason

        # Passenger rows are never deleted. Each parent registration gets
        # one cancellation event of type "trip", which is what marks its
        # passengers "Cancelled (trip)" (status "cancelled" + that event).
        by_parent = {}
        for p in active_regs:
            by_parent.setdefault(p.registration_id, []).append(p)

        for parent_id, rows in by_parent.items():
            event_id = None
            parent = db.session.get(Registration, parent_id) if parent_id else None
            if parent is not None:
                event = RegistrationCancellation(
                    registration_id=parent.id,
                    cancel_type="trip",
                    reason=reason,
                    cancelled_at=now,
                )
                db.session.add(event)
                db.session.flush()
                event_id = event.id
            for p in rows:
                p.status = "cancelled"
                p.cancelled_at = now
                p.cancellation_id = event_id
            db.session.flush()
            if parent is not None:
                registration_service.refresh_parent_status(
                    parent, parent.passengers, now
                )

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return _delay_response(
            False, trip,
            {"form": "Could not cancel the trip. Please try again."}, 500,
        )

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_TRIP_CANCELLED,
        details=(
            "Cancelled a scheduled trip. "
            + activity_log_service.describe_trip(trip)
            + f" | Passengers affected: {passenger_count}"
            + f" | Reason: {reason}"
        ),
    )

    # Only after the database update has succeeded.
    notified, announced = 0, False
    if wants_notice:
        message = (
            f"Your {departure_label} trip is cancelled "
            f"({_reason_phrase(reason)}). "
            "Please rebook online or at the terminal."
        )
        notified, announced = _notify_passengers(
            trip, contacts, message,
            notice_type=Announcement.TYPE_CANCELLATION,
            title="Trip cancelled",
            expires_at=trip.departure_time.strftime("%Y-%m-%dT%H:%M"),
            send_announcement=send_announcement,
            send_sms=send_sms,
        )

    if notified:
        flash(
            f"Trip cancelled. {notified} passenger"
            f"{'' if notified == 1 else 's'} notified by SMS.",
            "success",
        )
    elif announced:
        flash("Trip cancelled. Announcement published.", "success")
    else:
        flash("Trip cancelled.", "success")
    if wants_notice:
        warning = _notice_warning(
            notified, announced,
            send_announcement=send_announcement, send_sms=send_sms,
        )
        if warning:
            flash(warning, "warning")

    return _delay_response(True, trip)


@manifests_bp.route("/manifests/<int:trip_id>/pending/<int:reg_id>/approve", methods=["POST"])
@login_required
def approve_registration(trip_id, reg_id):
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    registration = PendingRegistration.query.get_or_404(reg_id)
    if registration.trip_id != trip.id:
        # A registration belongs to one trip; a mismatched URL must not be
        # able to act on it (e.g. on a registration of a departed trip).
        abort(404)

    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned

    current_count = _confirmed_count(trip.id)

    if current_count >= trip.boat.capacity:
        manifest_service.sync_trip_status(trip, current_count)
        db.session.commit()
        flash("Cannot approve — trip is at full capacity.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    duplicate = manifest_service.find_duplicate_message(
        trip.id,
        registration.full_name,
        age=registration.age,
        contact_number=registration.contact_number,
    )
    if duplicate:
        db.session.rollback()
        flash(f"Cannot approve \u2014 {duplicate} Reject this registration if it is a repeat.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    new_entry = ManifestEntry(
        trip_id=trip.id,
        full_name=manifest_service.clean_name(registration.full_name),
        age=registration.age,
        address=registration.address,
        contact_number=registration.contact_number,
        passenger_type=registration.passenger_type,
        source="online",
        registration_id=registration.registration_id,
    )
    db.session.add(new_entry)

    registration.status = "approved"

    db.session.flush()
    registration_service.mark_registration_approved(registration.registration)
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_PASSENGER_APPROVED,
        details=f"Approved {registration.full_name} for trip #{trip.id}.",
    )

    flash(f"{registration.full_name} approved and added to manifest.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/pending/<int:reg_id>/reject", methods=["POST"])
@login_required
def reject_registration(trip_id, reg_id):
    trip = Trip.query.get_or_404(trip_id)
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned
    registration = PendingRegistration.query.get_or_404(reg_id)
    if registration.trip_id != trip.id:
        abort(404)
    registration.status = "rejected"
    db.session.flush()
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_PASSENGER_REJECTED,
        details=f"Rejected {registration.full_name} for trip #{trip_id}.",
    )

    flash(f"{registration.full_name}'s registration was rejected.", "success")
    return redirect(url_for("manifests.trip_detail", trip_id=trip_id))

def _selected_pending(trip):
    """The ticked registrations that are still pending on THIS trip."""
    ids = set()
    for raw in request.form.getlist("reg_ids"):
        if raw.isdigit():
            ids.add(int(raw))
    if not ids:
        return []
    return (
        PendingRegistration.query.filter(
            PendingRegistration.trip_id == trip.id,
            PendingRegistration.status == "pending",
            PendingRegistration.id.in_(ids),
        )
        .order_by(PendingRegistration.submitted_at)
        .all()
    )


@manifests_bp.route("/manifests/<int:trip_id>/pending/bulk-approve", methods=["POST"])
@login_required
def bulk_approve_registrations(trip_id):
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned

    back = redirect(url_for("manifests.trip_detail", trip_id=trip.id))
    registrations = _selected_pending(trip)
    if not registrations:
        flash("Select at least one pending registration.")
        return back

    capacity = trip.boat.capacity
    approved, skipped_full, skipped_duplicate = [], 0, 0

    for registration in registrations:
        # Same rules as a single approval, re-checked for every passenger so
        # the trip can never go over capacity or take a repeat.
        if _confirmed_count(trip.id) >= capacity:
            skipped_full += 1
            continue
        if manifest_service.find_duplicate_message(
            trip.id,
            registration.full_name,
            age=registration.age,
            contact_number=registration.contact_number,
        ):
            skipped_duplicate += 1
            continue

        db.session.add(ManifestEntry(
            trip_id=trip.id,
            full_name=manifest_service.clean_name(registration.full_name),
            age=registration.age,
            address=registration.address,
            contact_number=registration.contact_number,
            passenger_type=registration.passenger_type,
            source="online",
            registration_id=registration.registration_id,
        ))
        registration.status = "approved"
        db.session.flush()
        registration_service.mark_registration_approved(registration.registration)
        approved.append(registration)

    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    for registration in approved:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_PASSENGER_APPROVED,
            details=f"Approved {registration.full_name} for trip #{trip.id}.",
        )

    if approved:
        flash(
            f"{len(approved)} registration{'' if len(approved) == 1 else 's'} "
            "approved and added to manifest.",
            "success",
        )
    skipped = []
    if skipped_full:
        skipped.append(f"{skipped_full} not approved: trip is at full capacity")
    if skipped_duplicate:
        skipped.append(
            f"{skipped_duplicate} not approved: already on this manifest "
            "(reject them if they are repeats)"
        )
    if skipped:
        if approved:
            flash("; ".join(skipped) + ".", "warning")
        else:
            flash("; ".join(skipped) + ".")
    return back


@manifests_bp.route("/manifests/<int:trip_id>/pending/bulk-reject", methods=["POST"])
@login_required
def bulk_reject_registrations(trip_id):
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned

    back = redirect(url_for("manifests.trip_detail", trip_id=trip.id))
    registrations = _selected_pending(trip)
    if not registrations:
        flash("Select at least one pending registration.")
        return back

    for registration in registrations:
        registration.status = "rejected"
    db.session.flush()
    manifest_service.sync_trip_status(trip, _confirmed_count(trip.id))
    db.session.commit()

    for registration in registrations:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_PASSENGER_REJECTED,
            details=f"Rejected {registration.full_name} for trip #{trip.id}.",
        )

    flash(
        f"{len(registrations)} registration"
        f"{'' if len(registrations) == 1 else 's'} rejected.",
        "success",
    )
    return back


@manifests_bp.route("/manifests/<int:trip_id>/confirm-departure", methods=["POST"])
@login_required
def confirm_departure(trip_id):
    trip = Trip.query.filter_by(id=trip_id).with_for_update().first_or_404()
    blocked = _block_if_read_only(trip)
    if blocked:
        return blocked
    unassigned = _block_if_unassigned(trip)
    if unassigned:
        return unassigned
    audit_confirmed = request.form.get("audit_confirmed")

    if not audit_confirmed:
        flash("You must confirm the Coast Guard audit before departure.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    if trip.status in ["Cancelled", "Departed"]:
        flash("This trip cannot be departed — it is already Cancelled or Departed.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    # Unsafe conditions need an explicit confirmation (the modal on the trip
    # page sends unsafe_confirmed). Checked here too, so the modal cannot be
    # skipped and conditions that turned Unsafe after the page loaded are caught.
    unsafe_now = _conditions_unsafe()
    if unsafe_now and not request.form.get("unsafe_confirmed"):
        flash(
            "Conditions are currently Unsafe. Review them and confirm "
            "again to depart anyway."
        )
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    trip.coast_guard_audit_confirmed = True
    trip.status = "Departed"
    trip.departed_at = db.func.now()
    # Freeze the advisory as it is right now, so the success page keeps
    # showing the condition at departure instead of the live one.
    trip.departure_conditions = _snapshot_departure_conditions()
    db.session.commit()

    # Audit trail: which account confirmed the departure. The departure is
    # already committed, so a logging problem must not turn it into an error.
    try:
        admin_name = session.get("full_name", "Unknown")
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=admin_name,
            action=activity_log_service.ACTION_TRIP_DEPARTED,
            details=(
                "Confirmed trip departure (Coast Guard audit confirmed). "
                + activity_log_service.describe_trip(trip)
                + f" | Confirmed by: {admin_name}"
                + f" (account ID {session.get('user_id')})"
                + (
                    " | Departed under UNSAFE conditions (operator confirmed)"
                    if unsafe_now else ""
                )
            ),
        )
    except Exception:
        current_app.logger.exception(
            "Could not write the departure activity log for trip %s", trip.id
        )

    return redirect(url_for("manifests.departure_success", trip_id=trip.id))


def _build_manifest_detail(trip):
    """Builds the dict pdf_service.build_manifest_pdf() expects."""
    entries = (
        ManifestEntry.query.filter_by(trip_id=trip.id)
        .order_by(ManifestEntry.check_in_time)
        .all()
    )
    when = trip.departure_time

    passenger_list = [
        {
            "no": index,
            "full_name": e.full_name,
            "age": e.age,
            "address": e.address,
            "check_in_label": (
                e.check_in_time.strftime("%I:%M %p") if e.check_in_time else "-"
            ),
            "passenger_type": e.passenger_type,
        }
        for index, e in enumerate(entries, start=1)
    ]

    return {
        "date_label": f"{when.strftime('%B')} {when.day}, {when.year}",
        "time_label": when.strftime("%I:%M %p").lstrip("0"),
        "capacity": trip.boat.capacity if trip.boat else None,
        "passengers": len(entries),
        "boat_name": trip.boat.name if trip.boat else None,
        "status": trip.status,
        "driver_name": trip.operator_name,
        "crew_name": trip.crew_name,
        "passenger_list": passenger_list,
    }


@manifests_bp.route("/manifests/<int:trip_id>/export-pdf")
@login_required
def export_pdf(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    detail = _build_manifest_detail(trip)

    pdf_bytes = pdf_service.build_manifest_pdf(detail)
    filename = pdf_service.manifest_filename(detail)

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Water level limits (metres). Outside LOW..HIGH is Unsafe; within
# CAUTION_MARGIN of either limit is Caution; anything else is Safe.
WATER_LEVEL_LOW_M = 10.50
WATER_LEVEL_HIGH_M = 12.50
WATER_LEVEL_CAUTION_MARGIN_M = 0.50


def _water_level_conditions(reading):
    """Fallback: status from the latest water level reading alone.

    status is one of: none, safe, caution, unsafe (the template colours the
    banner from it)."""
    level = getattr(reading, "water_level_m", None) if reading else None
    if level is None:
        return {
            "status": "none",
            "label": "Not recorded",
            "message": "Environmental conditions were not recorded for this trip.",
        }

    level = float(level)
    text = f"{level:.2f} m"

    if level < WATER_LEVEL_LOW_M:
        return {
            "status": "unsafe",
            "label": "Unsafe",
            "message": f"Water level is {text}, critically low. Verify conditions before future departures.",
        }
    if level > WATER_LEVEL_HIGH_M:
        return {
            "status": "unsafe",
            "label": "Unsafe",
            "message": f"Water level is {text}, critically high. Verify conditions before future departures.",
        }
    if level < WATER_LEVEL_LOW_M + WATER_LEVEL_CAUTION_MARGIN_M:
        return {
            "status": "caution",
            "label": "Caution",
            "message": f"Water level is {text}, close to the lower limit. Keep monitoring conditions.",
        }
    if level > WATER_LEVEL_HIGH_M - WATER_LEVEL_CAUTION_MARGIN_M:
        return {
            "status": "caution",
            "label": "Caution",
            "message": f"Water level is {text}, close to the upper limit. Keep monitoring conditions.",
        }
    return {
        "status": "safe",
        "label": "Safe",
        "message": f"Water level is {text}, within safe limits.",
    }


def _condition_level(status):
    """Map the dashboard's status text to none/safe/caution/unsafe."""
    text = str(status or "").strip().lower()
    if not text or "unavailable" in text:
        return None
    # "unsafe" contains "safe", so it must be checked first.
    if any(word in text for word in ("unsafe", "danger", "hazard", "avoid")):
        return "unsafe"
    if any(word in text for word in ("caution", "advisory", "moderate", "warning")):
        return "caution"
    if any(word in text for word in ("safe", "normal", "good", "clear")):
        return "safe"
    return None


def _departure_conditions(reading):
    """Banner for the departure success page.

    Uses the same overall condition as the dashboard / timetable
    (scheduling_service.get_current_overall_condition) so the two always
    agree. Only if that is unavailable does it fall back to the latest
    water level reading on its own."""
    try:
        condition = scheduling_service.get_current_overall_condition() or {}
    except Exception:
        current_app.logger.exception("Overall condition lookup failed")
        condition = {}

    level = _condition_level(condition.get("status"))
    if level:
        label = {"safe": "Safe", "caution": "Caution", "unsafe": "Unsafe"}[level]
        detail = next(
            (
                str(condition[key]).strip()
                for key in ("message", "summary", "description", "reason")
                if condition.get(key)
            ),
            "",
        )
        return {
            "status": level,
            "label": label,
            "message": detail or f"Overall environmental conditions are rated {label.lower()}.",
        }

    return _water_level_conditions(reading)


def _snapshot_departure_conditions():
    """The banner as it is at this moment, as a JSON-safe dict.

    Called when a departure is confirmed. It must never block the departure,
    so any failure is logged and stored as "not recorded"."""
    try:
        latest_reading = EnvironmentalReading.query.order_by(
            EnvironmentalReading.retrieved_at.desc()
        ).first()
        conditions = _departure_conditions(latest_reading)
        return {
            "status": str(conditions["status"]),
            "label": str(conditions["label"]),
            "message": str(conditions["message"]),
        }
    except Exception:
        current_app.logger.exception("Could not snapshot departure conditions")
        return None


def _recorded_departure_conditions(trip):
    """The banner recorded when `trip` departed. Never the live condition:
    trips with no recorded snapshot show "not recorded"."""
    snap = trip.departure_conditions
    if (
        isinstance(snap, dict)
        and snap.get("status") in ("none", "safe", "caution", "unsafe")
        and snap.get("message")
    ):
        return {
            "status": snap["status"],
            "label": snap.get("label", ""),
            "message": snap["message"],
        }
    return {
        "status": "none",
        "label": "Not recorded",
        "message": "No advisory was recorded for this departure.",
    }


@manifests_bp.route("/manifests/<int:trip_id>/departure-success")
@login_required
def departure_success(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    latest_reading = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).first()

    conditions = _recorded_departure_conditions(trip)
    conditions_message = conditions["message"]

    return render_template(
        "manifests/departure_success.html",
        trip=trip,
        conditions=conditions,
        conditions_message=conditions_message,
        latest_reading=latest_reading,
        passenger_count=_confirmed_count(trip.id),
        confirmed_by=session.get("full_name"),
        active_page="manifests",
    )