from datetime import date, datetime
from flask import current_app, render_template, request
from app.blueprints.dashboard import dashboard_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.trip import Trip
from app.models.manifest_entry import ManifestEntry
from app.models.environmental_reading import EnvironmentalReading
from app.services import scheduling_service, settings_service


@dashboard_bp.route("/dashboard")
@login_required
def index():
    today = date.today()

    all_trips_today = Trip.query.filter(
        db.func.date(Trip.departure_time) == today
    ).order_by(Trip.departure_time).all()

    scheduled_trips = Trip.query.filter(
        Trip.departure_time >= datetime.now(),
        Trip.status.notin_(["Cancelled", "Departed"]),
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

    latest_reading = (
        EnvironmentalReading.query
        .filter(EnvironmentalReading.water_level_m.isnot(None))
        .order_by(EnvironmentalReading.retrieved_at.desc())
        .first()
    )

    # Scheduling Decision-Support is read-only. The selected trip is used
    # only as context for the operator; no Trip or manifest row is changed.
    selected_trip = None
    scheduling_assessment = None
    trip_id = request.args.get("trip_id", type=int)
    if trip_id is not None:
        selected_trip = db.session.get(Trip, trip_id)
        if selected_trip is not None:
            scheduling_assessment = scheduling_service.get_assessment()


    if latest_reading is None:
        safety_status = "pending"
        safety_label = "Pending Setup"
    elif latest_reading.water_level_m < 10.50:
        safety_status = "critical-low"
        safety_label = "CRITICAL LOW"
    elif latest_reading.water_level_m > 12.50:
        safety_status = "critical-high"
        safety_label = "CRITICAL HIGH"
    else:
        safety_status = "clear"
        safety_label = "CLEAR"

    # pass these into render_template():
    # safety_status=safety_status, safety_label=safety_label, latest_reading=latest_reading

    return render_template(
        "dashboard/index.html",
        trips=all_trips_today,
        scheduled_trips=scheduled_trips,
        active_trips_count=active_trips_count,
        passengers_today=passengers_today,
        boarding_now_count=boarding_now_count,
        boarding_now_boat=boarding_now_boat,
        safety_status=safety_status,
        safety_label=safety_label,
        latest_reading=latest_reading,
        selected_trip=selected_trip,
        scheduling_assessment=scheduling_assessment,
        refresh_interval_seconds=settings_service.get_refresh_interval_seconds(),
        active_page="dashboard"
    )