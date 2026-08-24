from datetime import date
from flask import render_template
from app.blueprints.dashboard import dashboard_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.trip import Trip
from app.models.manifest_entry import ManifestEntry

@dashboard_bp.route("/dashboard")
@login_required
def index():
    today = date.today()

    all_trips_today = Trip.query.filter(
        db.func.date(Trip.departure_time) == today
    ).order_by(Trip.departure_time).all()

    active_trips_count = Trip.query.filter(
        Trip.status.in_(["Open", "Boarding", "Delayed"])
    ).count()

    trip_ids_today = [t.id for t in all_trips_today]
    passengers_today = 0
    if trip_ids_today:
        passengers_today = ManifestEntry.query.filter(
            ManifestEntry.trip_id.in_(trip_ids_today)
        ).count()

    boarding_trip = Trip.query.filter_by(status="Boarding").first()
    boarding_now_count = 0
    boarding_now_boat = None
    if boarding_trip:
        boarding_now_count = ManifestEntry.query.filter_by(trip_id=boarding_trip.id).count()
        boarding_now_boat = boarding_trip.boat.name if boarding_trip.boat else None

    return render_template(
        "dashboard/index.html",
        trips=all_trips_today,
        active_trips_count=active_trips_count,
        passengers_today=passengers_today,
        boarding_now_count=boarding_now_count,
        boarding_now_boat=boarding_now_boat,
        active_page="dashboard"
    )