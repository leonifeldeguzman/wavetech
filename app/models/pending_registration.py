from app.extensions import db
import random
import string

def generate_reference_code():
    year = "2026"
    digits = "".join(random.choices(string.digits, k=6))
    return f"WT-{year}-{digits}"

class PendingRegistration(db.Model):
    __tablename__ = "pending_registrations"

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.Integer, db.ForeignKey("trips.id"), nullable=False)

    reference_code = db.Column(db.String(20), unique=True, nullable=False, default=generate_reference_code)

    full_name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    address = db.Column(db.String(255), nullable=True)
    contact_number = db.Column(db.String(20), nullable=True)
    passenger_type = db.Column(db.String(20), nullable=False, default="Regular")

    submitted_at = db.Column(db.DateTime, server_default=db.func.now())
    status = db.Column(db.String(10), nullable=False, default="pending")
    # pending, approved, rejected

    trip = db.relationship("Trip", backref="pending_registrations")

    def __repr__(self):
        return f"<PendingRegistration {self.reference_code}>"