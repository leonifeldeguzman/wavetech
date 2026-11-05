"""Server-side state for identity verification (rate limiting + tokens)."""
from app.extensions import db
from app.utils.ph_clock import ph_now


class VerificationThrottle(db.Model):
    """Failed-attempt counter + cooldown, keyed by scope
    (``ref:<reference>`` or ``ip:<hash>``). Stored in the DB so limits hold
    across workers/restarts and cannot be bypassed by clearing cookies."""
    __tablename__ = "verification_throttles"

    id = db.Column(db.Integer, primary_key=True)
    scope_key = db.Column(db.String(100), nullable=False, unique=True)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    window_started_at = db.Column(db.DateTime, nullable=False, default=ph_now)
    locked_until = db.Column(db.DateTime, nullable=True)


class CancellationToken(db.Model):
    """Short-lived, single-use proof that the holder passed verification
    for ONE registration. Only a SHA-256 hash of the token is stored."""
    __tablename__ = "cancellation_tokens"

    id = db.Column(db.Integer, primary_key=True)
    registration_id = db.Column(
        db.Integer, db.ForeignKey("registrations.id"), nullable=False, index=True
    )
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=ph_now)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)