"""
Parent/child registration, identity verification and cancellation.

All business rules live here so every route enforces them identically.
Route-specific helpers (name masking, "has the trip left", trip re-opening)
are injected as callables to avoid a circular import with the blueprint.

Lock order (always): trip -> registration -> token -> passengers. The
registration flow already locks the trip first, so this cannot deadlock
against it.
"""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.trip import Trip
from app.models.pending_registration import (
    PendingRegistration,
    generate_reference_code,
)
from app.models.registration import (
    Registration,
    RegistrationCancellation,
    REG_PENDING,
    REG_APPROVED,
    REG_REJECTED,
    REG_CANCELLED,
)
from app.models.cancellation_security import (
    VerificationThrottle,
    CancellationToken,
)
from app.utils.ph_clock import ph_now

# Passenger statuses
P_PENDING, P_APPROVED, P_REJECTED, P_CANCELLED = (
    "pending", "approved", "rejected", "cancelled"
)
ACTIVE_STATUSES = (P_PENDING, P_APPROVED)      # hold / held a seat
# Only pending passengers can be cancelled online. Approved passengers
# already have a ManifestEntry (which has no link back to the registration),
# so they must be handled by staff, exactly as before this change.
CANCELLABLE_STATUSES = (P_PENDING,)

# Verification policy
REF_MAX_FAILURES = 5          # per reference code
IP_MAX_FAILURES = 20          # per client address
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
TOKEN_TTL = timedelta(minutes=10)

GENERIC_VERIFY_MESSAGE = "The verification details do not match our records."
VERIFICATION_REQUIRED_MESSAGE = "Please verify your identity first."

REFERENCE_CODE_MAX_ATTEMPTS = 5


def _now():
    """Single time source (tests monkeypatch this)."""
    return ph_now()


class ServiceResult:
    def __init__(self, ok, code=None, message=None, status=200,
                 data=None, retry_after=None, notify=None):
        self.ok = ok
        self.code = code
        self.message = message
        self.status = status
        self.data = data
        self.retry_after = retry_after
        # Private, server-side-only info for best-effort SMS/logging.
        # Never serialise this into a response.
        self.notify = notify


def _fail(code, message, status, **kw):
    return ServiceResult(False, code, message, status, **kw)


# ---------------------------------------------------------------------------
# Reference codes / parent creation
# ---------------------------------------------------------------------------

def normalize_reference(reference_code):
    return str(reference_code or "").strip().upper()[:30]


def create_parent_registration(trip, registration_type):
    """Create and flush the parent row with a unique reference code.
    Returns the Registration, or None if no unique code could be made.
    Uses a SAVEPOINT so a collision does not release the trip lock."""
    for _ in range(REFERENCE_CODE_MAX_ATTEMPTS):
        code = generate_reference_code()
        if (Registration.query.filter_by(reference_code=code).first()
                or PendingRegistration.query.filter_by(reference_code=code).first()):
            continue
        parent = Registration(
            reference_code=code,
            trip_id=trip.id,
            registration_type=registration_type,
            status=REG_PENDING,
        )
        try:
            with db.session.begin_nested():
                db.session.add(parent)
                db.session.flush()
            return parent
        except IntegrityError:
            continue
    return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def compute_parent_status(passengers):
    """Overall status, derived from the passengers who are still active.

    Cancelled passengers do not change it: a group with one cancelled
    passenger and one awaiting approval is still "pending". It becomes
    "cancelled" only when no active (pending/approved) passenger remains."""
    statuses = [p.status for p in passengers]
    active = [s for s in statuses if s in ACTIVE_STATUSES]

    if active:
        return REG_PENDING if P_PENDING in active else REG_APPROVED
    if P_CANCELLED in statuses:
        return REG_CANCELLED
    if statuses and all(s == P_REJECTED for s in statuses):
        return REG_REJECTED
    return REG_PENDING


def refresh_parent_status(parent, passengers, now=None):
    """Recompute and store the parent's status (also usable by admin code)."""
    parent.status = compute_parent_status(passengers)
    if parent.status == REG_CANCELLED:
        parent.cancelled_at = parent.cancelled_at or (now or _now())
    return parent.status


def mark_registration_approved(parent, now=None):
    """Called after a passenger of ``parent`` has been approved by an admin.

    Records ``approved_at`` the first time only (an existing value is never
    overwritten) and re-derives the parent status. ``created_at`` is never
    touched. Safe to call with None (legacy passenger with no parent)."""
    if parent is None:
        return
    now = now or _now()
    if parent.approved_at is None:
        parent.approved_at = now
    refresh_parent_status(parent, parent.passengers, now)


def load_registration(reference_code):
    """Return (parent, passengers) or (None, [])."""
    ref = normalize_reference(reference_code)
    parent = Registration.query.filter_by(reference_code=ref).first()
    if not parent:
        return None, []
    passengers = (
        PendingRegistration.query
        .filter_by(registration_id=parent.id)
        .order_by(PendingRegistration.id.asc())
        .all()
    )
    return parent, passengers


def cancellation_eligibility(passengers, trip, trip_has_left):
    """Returns (can_cancel, reason_code)."""
    if any(p.status in CANCELLABLE_STATUSES for p in passengers):
        if trip and trip_has_left(trip):
            return False, "TRIP_DEPARTED"
        return True, None
    if passengers and not any(p.status in ACTIVE_STATUSES for p in passengers) \
            and any(p.status == P_CANCELLED for p in passengers):
        return False, "ALREADY_CANCELLED"
    return False, "NOT_CANCELLABLE"


# ---------------------------------------------------------------------------
# Serialisation (privacy is enforced HERE, at the response level)
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = {
    "contact_number", "guardian_contact", "guardian_name", "phone",
    "mobile", "address", "full_name", "age", "last_four",
}


def _assert_public_safe(value):
    """Defensive check: no raw PII key may ever reach a public response."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _FORBIDDEN_KEYS:
                raise RuntimeError(f"Private field '{k}' in public response")
            _assert_public_safe(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _assert_public_safe(v)
    return value


def serialize_active_passengers(passengers, mask_name):
    return _assert_public_safe([
        {
            "id": p.id,
            "name": mask_name(p.full_name),
            "status": p.status,
            "cancellable": p.status in CANCELLABLE_STATUSES,
        }
        for p in passengers
        if p.status in ACTIVE_STATUSES
    ])


def serialize_status(parent, passengers, trip, mask_name, trip_has_left):
    """Public status payload. Whitelist-built: contact numbers (full or
    masked), addresses and real names are never included."""
    shown = (trip.new_departure_time or trip.departure_time) if trip else None
    can_cancel, reason = cancellation_eligibility(passengers, trip, trip_has_left)
    active = serialize_active_passengers(passengers, mask_name)

    data = {
        "reference_code": parent.reference_code,
        "status": compute_parent_status(passengers),   # live, never stale
        "registration_type": parent.registration_type,
        "trip": {
            "trip_id": parent.trip_id,
            "trip_date": shown.strftime("%B %d, %Y") if shown else None,
            "departure_time": (
                shown.strftime("%I:%M %p").lstrip("0") if shown else None
            ),
            "route_origin": trip.route_origin if trip else None,
            "route_destination": trip.route_destination if trip else None,
            "route": (
                f"{trip.route_origin} to {trip.route_destination}" if trip else None
            ),
            "trip_status": trip.status if trip else None,
        },
        "trip_id": parent.trip_id,
        "passenger_count": len(passengers),
        "active_passenger_count": len(active),
        "cancelled_passenger_count": sum(
            1 for p in passengers if p.status == P_CANCELLED
        ),
        "active_passengers": active,
        "can_cancel": can_cancel,
        "cancellation_block_reason": reason,
    }
    return _assert_public_safe(data)


def serialize_cancellation_history(parent, passengers, mask_name):
    """History is kept apart from active passengers. The free-text reason
    is deliberately omitted: this endpoint is unauthenticated."""
    by_event = {}
    for p in passengers:
        if p.cancellation_id:
            by_event.setdefault(p.cancellation_id, []).append(p)

    events = []
    for ev in parent.cancellations:
        affected = by_event.get(ev.id, [])
        events.append({
            "cancelled_at": ev.cancelled_at.isoformat() if ev.cancelled_at else None,
            "cancel_type": ev.cancel_type,
            "passenger_count": len(affected),
            "passengers": [
                {"id": p.id, "name": mask_name(p.full_name)} for p in affected
            ],
        })
    return _assert_public_safe({
        "reference_code": parent.reference_code,
        "cancellations": events,
    })


# ---------------------------------------------------------------------------
# Identity verification (last four digits) with throttling
# ---------------------------------------------------------------------------

def _last_four(number):
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _throttle_row(key, now):
    """Fetch (creating if needed) and lock a throttle row, expiring stale
    state. Callers must commit/rollback to release the lock."""
    q = VerificationThrottle.query.filter_by(scope_key=key)
    row = q.with_for_update().first()
    if row is None:
        try:
            with db.session.begin_nested():
                db.session.add(VerificationThrottle(
                    scope_key=key, failed_count=0, window_started_at=now))
        except IntegrityError:
            pass
        row = q.with_for_update().first()

    if row.locked_until is not None and row.locked_until <= now:
        row.locked_until = None
        row.failed_count = 0
        row.window_started_at = now
    elif row.locked_until is None and now - row.window_started_at > FAILURE_WINDOW:
        row.failed_count = 0
        row.window_started_at = now
    return row


def _seconds_until(moment, now):
    return max(int((moment - now).total_seconds()), 1)


def verify_identity(reference_code, last_four, client_ip, trip_has_left):
    """Check the last four digits of the registration's contact number.

    Success -> single-use expiring token (only its hash is stored).
    Failure -> one generic message whether or not the reference exists.
    """
    now = _now()
    ref = normalize_reference(reference_code)
    ip_hash = hashlib.sha256(str(client_ip or "?").encode()).hexdigest()[:32]

    try:
        # Fixed order (ref, ip) so concurrent requests cannot deadlock.
        ref_row = _throttle_row(f"ref:{ref}", now)
        ip_row = _throttle_row(f"ip:{ip_hash}", now)
        rows = [(ref_row, REF_MAX_FAILURES), (ip_row, IP_MAX_FAILURES)]

        # 1. Cooldown check comes first and never looks at the digits, so a
        #    locked caller learns nothing about whether a guess was right.
        locked = [r.locked_until for r, _ in rows if r.locked_until]
        if locked:
            retry = _seconds_until(max(locked), now)
            db.session.commit()
            return _fail(
                "TOO_MANY_ATTEMPTS",
                "Too many attempts. Please try again later.",
                429, retry_after=retry,
            )

        # 2. Malformed input is rejected without counting as a guess.
        digits = str(last_four or "").strip()
        if not re.fullmatch(r"\d{4}", digits):
            db.session.commit()
            return _fail("INVALID_INPUT", "Please enter exactly four digits.", 400)

        # 3. Compare. A missing reference takes the same path as a wrong code.
        parent, passengers = load_registration(ref)
        candidates = {
            _last_four(p.contact_number) for p in passengers if p.contact_number
        } - {None}
        matched = False
        for cand in (candidates or {"----"}):
            if hmac.compare_digest(cand, digits):
                matched = bool(parent) and cand != "----"

        if not matched:
            engaged = False
            for row, limit in rows:
                row.failed_count += 1
                if row.failed_count >= limit:
                    row.locked_until = now + LOCKOUT
                    engaged = True
            db.session.commit()
            if engaged:
                return _fail(
                    "TOO_MANY_ATTEMPTS",
                    "Too many attempts. Please try again later.",
                    429, retry_after=int(LOCKOUT.total_seconds()),
                )
            return _fail("VERIFICATION_FAILED", GENERIC_VERIFY_MESSAGE, 403)

        # 4. Verified. Now (and only now) report registration state.
        ref_row.failed_count = 0
        trip = db.session.get(Trip, parent.trip_id)
        can_cancel, reason = cancellation_eligibility(
            passengers, trip, trip_has_left)
        if not can_cancel:
            db.session.commit()
            msg = {
                "TRIP_DEPARTED": "This trip has already departed and can no longer be cancelled.",
                "ALREADY_CANCELLED": "This registration has already been cancelled.",
            }.get(reason, "There are no pending passengers available for cancellation.")
            return _fail(reason, msg, 409)

        # One live token per registration: retire any earlier ones.
        (CancellationToken.query
            .filter_by(registration_id=parent.id, consumed_at=None)
            .update({"consumed_at": now}, synchronize_session=False))

        raw = secrets.token_urlsafe(32)
        db.session.add(CancellationToken(
            registration_id=parent.id,
            token_hash=_hash_token(raw),
            created_at=now,
            expires_at=now + TOKEN_TTL,
        ))
        db.session.commit()
        return ServiceResult(True, status=200, data={
            "token": raw,
            "reference_code": parent.reference_code,
            "expires_in": int(TOKEN_TTL.total_seconds()),
        })
    except Exception:
        db.session.rollback()
        raise


def _hash_token(raw):
    return hashlib.sha256(str(raw or "").encode()).hexdigest()


# ---------------------------------------------------------------------------
# Cancellation (solo / full / partial)
# ---------------------------------------------------------------------------

def _parse_ids(raw):
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for v in raw:
        if isinstance(v, bool):
            return None
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            return None
    return sorted(set(out))


def cancel_passengers(reference_code, raw_token, cancel_type, passenger_ids,
                      reason, trip_has_left, reopen_trip):
    """Cancel passengers under one parent registration.

    cancel_type: "solo" | "full" | "partial". Authorisation is the
    verification token alone; everything else is re-checked under locks.
    """
    ref = normalize_reference(reference_code)

    # ---- input validation (no DB access) --------------------------------
    if cancel_type not in ("solo", "full", "partial"):
        return _fail("INVALID_CANCELLATION_TYPE",
                     "Invalid cancellation type.", 400)

    ids = None
    if cancel_type == "partial":
        ids = _parse_ids(passenger_ids)
        if ids is None:
            return _fail("INVALID_PASSENGER_SELECTION",
                         "Please select at least one passenger.", 400)
    elif passenger_ids is not None:
        return _fail("INVALID_PASSENGER_SELECTION",
                     "Passenger selection is only valid for partial cancellation.", 400)

    reason = str(reason or "").strip()[:200] or None

    # ---- cheap, unlocked authorisation pre-check ------------------------
    parent0 = Registration.query.filter_by(reference_code=ref).first()
    token0 = (
        CancellationToken.query.filter_by(token_hash=_hash_token(raw_token)).first()
        if raw_token else None
    )
    if not parent0 or not token0 or token0.registration_id != parent0.id:
        return _fail("VERIFICATION_REQUIRED", VERIFICATION_REQUIRED_MESSAGE, 403)

    now = _now()
    try:
        # ---- locks: trip -> registration -> token -> passengers ---------
        trip = Trip.query.filter_by(id=parent0.trip_id).with_for_update().first()
        parent = (Registration.query.filter_by(id=parent0.id)
                  .with_for_update().first())
        token = (CancellationToken.query.filter_by(id=token0.id)
                 .with_for_update().first())

        # Re-check under lock: this is what stops double submissions/replays.
        if token.consumed_at is not None or token.expires_at <= now:
            db.session.rollback()
            return _fail("VERIFICATION_REQUIRED", VERIFICATION_REQUIRED_MESSAGE, 403)

        passengers = (
            PendingRegistration.query.filter_by(registration_id=parent.id)
            .order_by(PendingRegistration.id.asc())
            .with_for_update().all()
        )
        by_id = {p.id: p for p in passengers}

        if trip and trip_has_left(trip):
            db.session.rollback()
            return _fail("TRIP_DEPARTED",
                         "This trip has already departed and can no longer be cancelled.",
                         409)

        # ---- choose the passengers to cancel ----------------------------
        if cancel_type == "partial":
            if parent.registration_type != "group":
                db.session.rollback()
                return _fail("INVALID_CANCELLATION_TYPE",
                             "Partial cancellation applies to group registrations only.",
                             400)
            if not set(ids).issubset(by_id):
                # Same answer for "does not exist" and "belongs to another
                # registration" so IDs cannot be probed.
                db.session.rollback()
                return _fail("INVALID_PASSENGER_SELECTION",
                             "Invalid passenger selection.", 400)
            selected = [by_id[i] for i in ids]
            if any(p.status == P_CANCELLED for p in selected):
                db.session.rollback()
                return _fail("ALREADY_CANCELLED",
                             "One or more selected passengers are already cancelled.", 409)
            if any(p.status not in CANCELLABLE_STATUSES for p in selected):
                db.session.rollback()
                return _fail("NOT_CANCELLABLE",
                             "One or more selected passengers can no longer be cancelled.",
                             409)
        else:
            if cancel_type == "solo" and parent.registration_type != "solo":
                db.session.rollback()
                return _fail("INVALID_CANCELLATION_TYPE",
                             "Solo cancellation applies to solo registrations only.", 400)
            selected = [p for p in passengers if p.status in CANCELLABLE_STATUSES]
            if not selected:
                db.session.rollback()
                if any(p.status == P_CANCELLED for p in passengers) and \
                        not any(p.status in ACTIVE_STATUSES for p in passengers):
                    return _fail("ALREADY_CANCELLED",
                                 "This registration has already been cancelled.", 409)
                return _fail("NOT_CANCELLABLE",
                             "There are no passengers available for cancellation.", 409)

        # ---- apply (nothing is deleted) ---------------------------------
        event = RegistrationCancellation(
            registration_id=parent.id,
            cancel_type=cancel_type,
            reason=reason,
            cancelled_at=now,
        )
        db.session.add(event)
        db.session.flush()

        for p in selected:
            p.status = P_CANCELLED
            p.cancelled_at = now
            p.cancellation_id = event.id

        token.consumed_at = now           # single use
        db.session.flush()

        refresh_parent_status(parent, passengers, now)

        if trip:
            reopen_trip(trip)             # flushes + frees "Full" if seats exist

        remaining = [p for p in passengers if p.status in ACTIVE_STATUSES]
        contact = next((p.contact_number for p in passengers if p.contact_number), None)

        # Plain snapshot BEFORE commit (ORM instances expire on commit).
        data = {
            "reference_code": parent.reference_code,
            "registration_status": parent.status,
            "cancel_type": cancel_type,
            "cancelled": [(p.id, p.full_name) for p in selected],
            "remaining": [(p.id, p.full_name) for p in remaining],
            "seats_released": len(selected),
            "trip_id": parent.trip_id,
        }
        notify = {"contact_number": contact, "reason": reason}

        db.session.commit()
        return ServiceResult(True, status=200, data=data, notify=notify)

    except Exception:
        db.session.rollback()
        raise