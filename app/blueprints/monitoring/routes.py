from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.blueprints.monitoring import monitoring_bp
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service, settings_service
from app.utils.decorators import login_required


@monitoring_bp.route("/monitoring")
@login_required
def index():
    # Get current environmental conditions
    llda_conditions = monitoring_service.get_llda_conditions()
    windy_conditions = monitoring_service.get_windy_conditions()

    # Get the latest environmental reading from any source
    latest = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).first()

    # Get the 10 most recent environmental readings
    history = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).limit(10).all()

    # Windy's weather condition can be suggested in the
    # manual entry form while still allowing the operator to edit it.
    windy_reading = windy_conditions["reading"]

    suggested_weather_condition = (
        windy_reading.weather_condition
        if windy_reading
        else None
    )

    return render_template(
        "monitoring/index.html",

        # Existing readings/history
        latest=latest,
        history=history,

        # -------------------------
        # LLDA DATA
        # -------------------------
        llda_status=llda_conditions["status"],
        llda_reading=llda_conditions["reading"],
        llda_message=llda_conditions["message"],

        # -------------------------
        # WINDY DATA
        # -------------------------
        windy_status=windy_conditions["status"],
        windy_reading=windy_conditions["reading"],
        windy_message=windy_conditions["message"],

        # Suggested value for manual entry
        suggested_weather_condition=suggested_weather_condition,

        # -------------------------
        # AUTO REFRESH
        # -------------------------
        refresh_interval_seconds=settings_service.get_refresh_interval_seconds(),

        # -------------------------
        # MONITORING MAP
        # -------------------------
        # Uses the same coordinates configured for Windy.
        # This keeps the map and Windy monitoring point consistent.
        monitoring_lat=current_app.config["MONITORING_LAT"],
        monitoring_lon=current_app.config["MONITORING_LON"],
        monitoring_location_label=current_app.config["MONITORING_LOCATION_LABEL"],

        # Active sidebar page
        active_page="monitoring",
    )


@monitoring_bp.route("/monitoring/add-reading", methods=["POST"])
@login_required
def add_reading():
    # Get values from manual entry form
    water_level = request.form.get("water_level_m")
    wave_height = request.form.get("wave_height_m")
    wind_speed = request.form.get("wind_speed_kmh")
    wind_direction = request.form.get("wind_direction")
    weather_condition = request.form.get("weather_condition")
    temperature = request.form.get("temperature_c")
    location_label = request.form.get("location_label")

    # Water level is required
    if not water_level:
        flash("Water level is required.")
        return redirect(url_for("monitoring.index"))

    # Create manual environmental reading
    new_reading = EnvironmentalReading(
        source="manual",

        # Save entered location, or use the default monitoring location
        location_label=(
            location_label
            or "Central Bay, Cardona, Rizal"
        ),

        water_level_m=float(water_level),

        wave_height_m=(
            float(wave_height)
            if wave_height
            else None
        ),

        wind_speed_kmh=(
            float(wind_speed)
            if wind_speed
            else None
        ),

        wind_direction=wind_direction or None,

        weather_condition=weather_condition or None,

        temperature_c=(
            float(temperature)
            if temperature
            else None
        ),

        entered_by_user_id=session.get("user_id"),
    )

    db.session.add(new_reading)
    db.session.commit()

    flash("Environmental reading recorded successfully.")

    return redirect(url_for("monitoring.index"))