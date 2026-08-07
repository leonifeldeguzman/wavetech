from datetime import date
from flask import render_template
from app.blueprints.dashboard import dashboard_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.trip import Trip

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

    return render_template(
        "dashboard/index.html",
        trips=all_trips_today,
        active_trips_count=active_trips_count
    )