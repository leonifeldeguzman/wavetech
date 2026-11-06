"""Parent registration (one per registration transaction, solo or group)."""
from app.extensions import db
from app.utils.ph_clock import ph_now

REGISTRATION_TYPES = ("solo", "group")

# Overall (parent) statuses
REG_PENDING = "pending"
REG_APPROVED = "approved"
REG_REJECTED = "rejected"
REG_CANCELLED = "cancelled"


class Registration(db.Model):
    """One row per registration transaction.

    Owns the reference code. Passengers live in ``pending_registrations``
    (the existing table) and point back here via ``registration_id``.
    """
    __tablename__ = "registrations"

    id = db.Column(db.Integer, primary_key=True)

    reference_code = db.Column(
        db.String(20), nullable=False, unique=True, index=True
    )

    trip_id = db.Column(
        db.Integer, db.ForeignKey("trips.id"), nullable=False, index=True
    )

    registration_type = db.Column(
        db.String(10), nullable=False, default="solo"
    )  # solo | group

    status = db.Column(
        db.String(20), nullable=False, default=REG_PENDING
    )  # pending | approved | rejected | cancelled

    # When the passenger submitted the pre-registration. Set once on insert
    # (PH wall-clock) and never rewritten.
    created_at = db.Column(db.DateTime, default=ph_now)
    # When an admin first approved the registration (PH wall-clock, same
    # convention as created_at). NULL until approved; legacy approved rows
    # stay NULL (not backfilled). Set once, never overwritten.
    approved_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "registration_type IN ('solo','group')",
            name="ck_registrations_type",
        ),
    )

    trip = db.relationship("Trip", backref="registrations")

    passengers = db.relationship(
        "PendingRegistration",
        back_populates="registration",
        order_by="PendingRegistration.id",
    )

    cancellations = db.relationship(
        "RegistrationCancellation",
        back_populates="registration",
        order_by="RegistrationCancellation.id",
    )

    def __repr__(self):
        return f"<Registration {self.reference_code} {self.registration_type} {self.status}>"


class RegistrationCancellation(db.Model):
    """One row per cancellation *event* (history). The passengers affected
    are the PendingRegistration rows whose ``cancellation_id`` points here;
    passenger rows are never deleted."""
    __tablename__ = "registration_cancellations"

    id = db.Column(db.Integer, primary_key=True)

    registration_id = db.Column(
        db.Integer,
        db.ForeignKey("registrations.id"),
        nullable=False,
        index=True,
    )

    cancel_type = db.Column(db.String(10), nullable=False)  # solo|full|partial
    reason = db.Column(db.String(200), nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=False, default=ph_now)

    registration = db.relationship("Registration", back_populates="cancellations")

    passengers = db.relationship(
        "PendingRegistration",
        back_populates="cancellation",
        order_by="PendingRegistration.id",
    )