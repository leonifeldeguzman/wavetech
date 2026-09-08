from datetime import datetime, timezone

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
from app.services import monitoring_service
from app.utils.decorators import login_required
from app.utils.timezone import to_naive_utc


@monitoring_bp.route("/monitoring")
@login_required
def index():
    llda_conditions = monitoring_service.get_llda_conditions()
    windy_conditions = monitoring_service.get_windy_conditions()

    # Get the latest environmental reading from any source.
    latest = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).first()

    # Get the 10 most recent environmental readings.
    history = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).limit(10).all()

    # Windy's derived weather condition becomes the suggested value
    # for the manual entry form. The admin can still change it.
    windy_reading = windy_conditions["reading"]

    suggested_weather_condition = (
        windy_reading.weather_condition
        if windy_reading
        else None
    )

    return render_template(
        "monitoring/index.html",
        latest=latest,
        history=history,

        # LLDA data
        llda_status=llda_conditions["status"],
        llda_reading=llda_conditions["reading"],
        llda_message=llda_conditions["message"],

        # Windy data
        windy_status=windy_conditions["status"],
        windy_reading=windy_conditions["reading"],
        windy_message=windy_conditions["message"],

        # Windy weather suggestion for manual entry
        suggested_weather_condition=suggested_weather_condition,

        # Dashboard refresh interval
        refresh_interval_seconds=current_app.config[
            "REFRESH_INTERVAL_SECONDS"
        ],

        active_page="monitoring",
    )


@monitoring_bp.route("/monitoring/add-reading", methods=["POST"])
@login_required
def add_reading():
    water_level = request.form.get("water_level_m")
    wave_height = request.form.get("wave_height_m")
    wind_speed = request.form.get("wind_speed_kmh")
    wind_direction = request.form.get("wind_direction")
    weather_condition = request.form.get("weather_condition")
    temperature = request.form.get("temperature_c")
    location_label = request.form.get("location_label")

    if not water_level:
        flash("Water level is required.")
        return redirect(url_for("monitoring.index"))

    new_reading = EnvironmentalReading(
        source="manual",

        # Use the entered location, or the default monitoring location.
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

        # Store manual reading timestamp as naive UTC,
        # matching the project's timestamp convention.
        recorded_at=to_naive_utc(
            datetime.now(timezone.utc)
        ),
    )

    db.session.add(new_reading)
    db.session.commit()

    flash("Environmental reading recorded successfully.")

    return redirect(url_for("monitoring.index"))