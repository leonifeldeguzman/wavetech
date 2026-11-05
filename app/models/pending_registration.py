from app.extensions import db
import random
import string
from datetime import datetime


def generate_reference_code():
    year = datetime.now().year
    digits = "".join(random.choices(string.digits, k=6))
    return f"WT-{year}-{digits}"


class PendingRegistration(db.Model):
    __tablename__ = "pending_registrations"

    id = db.Column(db.Integer, primary_key=True)

    trip_id = db.Column(
        db.Integer,
        db.ForeignKey("trips.id"),
        nullable=False
    )

    # Denormalised copy of Registration.reference_code, kept so existing
    # queries/admin code keep working. The parent Registration is the owner.
    # Multiple passenger rows share the same reference code.
    reference_code = db.Column(
        db.String(20),
        nullable=False,
        default=generate_reference_code,
        index=True
    )

    full_name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    address = db.Column(db.String(255), nullable=True)
    contact_number = db.Column(db.String(20), nullable=True)

    passenger_type = db.Column(
        db.String(20),
        nullable=False,
        default="Regular"
    )

    guardian_name = db.Column(db.String(100), nullable=True)
    guardian_contact = db.Column(db.String(20), nullable=True)

    submitted_at = db.Column(
        db.DateTime,
        server_default=db.func.now()
    )

    status = db.Column(
        db.String(10),
        nullable=False,
        default="pending"
    )

    # pending, approved, rejected, cancelled

    # --- Parent/child link and cancellation history (new) ---------------
    # nullable at the DB level only so the migration can backfill legacy
    # rows; every creation path sets it (see registration_service).
    registration_id = db.Column(
        db.Integer,
        db.ForeignKey("registrations.id"),
        nullable=True,
        index=True
    )

    cancelled_at = db.Column(db.DateTime, nullable=True)

    cancellation_id = db.Column(
        db.Integer,
        db.ForeignKey("registration_cancellations.id"),
        nullable=True
    )

    registration = db.relationship(
        "Registration",
        back_populates="passengers"
    )

    cancellation = db.relationship(
        "RegistrationCancellation",
        back_populates="passengers"
    )

    trip = db.relationship(
        "Trip",
        backref="pending_registrations"
    )

    def __repr__(self):
        return (
            f"<PendingRegistration "
            f"{self.reference_code} - {self.full_name}>"
        )