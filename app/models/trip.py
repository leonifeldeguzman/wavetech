from app.extensions import db

class Trip(db.Model):
    __tablename__ = "trips"

    id = db.Column(db.Integer, primary_key=True)
    boat_id = db.Column(db.Integer, db.ForeignKey("boats.id"), nullable=True)
    crew_name = db.Column(db.String(100), nullable=True)

    departure_time = db.Column(db.DateTime, nullable=False)
    route_origin = db.Column(db.String(100), nullable=False, default="Cabuyao Terminal")
    route_destination = db.Column(db.String(100), nullable=False, default="Talim Island")

    status = db.Column(db.String(20), nullable=False, default="Open")
    # Open, Boarding, Full, Delayed, Cancelled, Departed

    delay_reason = db.Column(db.String(255), nullable=True)
    cancel_reason = db.Column(db.String(255), nullable=True)
    new_departure_time = db.Column(db.DateTime, nullable=True)

    coast_guard_audit_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    departed_at = db.Column(db.DateTime, nullable=True)

    boat = db.relationship("Boat", backref="trips")

    def __repr__(self):
        return f"<Trip {self.departure_time} - {self.status}>"