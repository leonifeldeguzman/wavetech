from app import create_app
from app.extensions import db
from app.models.trip import Trip
from app.models.pending_registration import PendingRegistration

app = create_app()

with app.app_context():
    trip = Trip.query.filter_by(status="Open").first()

    if not trip:
        print("No 'Open' trip found — assign a boat to a trip first, or pick any trip manually.")
    else:
        reg1 = PendingRegistration(
            trip_id=trip.id,
            full_name="Joseph Lee",
            age=24,
            address="Brgy. Dita",
            contact_number="0917-123-4567",
            passenger_type="Regular"
        )
        reg2 = PendingRegistration(
            trip_id=trip.id,
            full_name="Sophia Kim",
            age=30,
            address="Brgy. Aplaya",
            contact_number="0918-765-4321",
            passenger_type="Regular"
        )
        db.session.add_all([reg1, reg2])
        db.session.commit()
        print(f"Seeded 2 pending registrations for trip at {trip.departure_time.strftime('%I:%M %p')}")