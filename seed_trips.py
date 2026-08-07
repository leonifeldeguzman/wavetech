from datetime import datetime, date
from app import create_app
from app.extensions import db
from app.models.boat import Boat
from app.models.trip import Trip

app = create_app()

with app.app_context():
    if Boat.query.first():
        print("Boats already exist — skipping boat seeding.")
    else:
        boats = [
            Boat(name="MB Horizon", capacity=25),
            Boat(name="MB Coral", capacity=15),
            Boat(name="MB Blue", capacity=10),
            Boat(name="MB Explorer", capacity=50),
        ]
        db.session.add_all(boats)
        db.session.commit()
        print("Boats seeded.")

    if Trip.query.first():
        print("Trips already exist — skipping trip seeding.")
    else:
        horizon = Boat.query.filter_by(name="MB Horizon").first()
        coral = Boat.query.filter_by(name="MB Coral").first()

        today = date.today()

        trips = [
            Trip(
                boat_id=horizon.id,
                crew_name="Capt. Pedro",
                departure_time=datetime.combine(today, datetime.strptime("09:30 AM", "%I:%M %p").time()),
                status="Departed"
            ),
            Trip(
                boat_id=coral.id,
                crew_name="Capt. Dela Cruz",
                departure_time=datetime.combine(today, datetime.strptime("10:00 AM", "%I:%M %p").time()),
                status="Boarding"
            ),
            Trip(
                boat_id=None,
                crew_name=None,
                departure_time=datetime.combine(today, datetime.strptime("12:00 PM", "%I:%M %p").time()),
                status="Open"
            ),
            Trip(
                boat_id=None,
                crew_name=None,
                departure_time=datetime.combine(today, datetime.strptime("04:00 PM", "%I:%M %p").time()),
                status="Open"
            ),
        ]
        db.session.add_all(trips)
        db.session.commit()
        print("Trips seeded.")