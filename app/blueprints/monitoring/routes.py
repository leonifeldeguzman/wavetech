from flask import render_template, request, redirect, url_for, flash
from app.blueprints.monitoring import monitoring_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from datetime import datetime, timezone
from app.services import monitoring_service


@monitoring_bp.route("/monitoring")
@login_required
def index():
    llda_conditions = monitoring_service.get_llda_conditions()
    windy_conditions = monitoring_service.get_windy_conditions()

    latest = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).first()

    history = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).limit(10).all()

    # Windy's derived condition becomes the pre-filled suggestion on the
    # manual entry form; the admin can still change it before submitting.
    windy_reading = windy_conditions["reading"]
    suggested_weather_condition = windy_reading.weather_condition if windy_reading else None

    return render_template(
        "monitoring/index.html",
        latest=latest,
        history=history,
        active_page="monitoring",
        llda_status=llda_conditions["status"],
        llda_reading=llda_conditions["reading"],
        llda_message=llda_conditions["message"],
        windy_status=windy_conditions["status"],
        windy_reading=windy_conditions["reading"],
        windy_message=windy_conditions["message"],
        suggested_weather_condition=suggested_weather_condition,
    )


@monitoring_bp.route("/monitoring/add-reading", methods=["POST"])
@login_required
def add_reading():
    from flask import session

    water_level = request.form.get("water_level_m")
    wave_height = request.form.get("wave_height_m")
    wind_speed = request.form.get("wind_speed_kmh")
    wind_direction = request.form.get("wind_direction")
    weather_condition = request.form.get("weather_condition")
    temperature = request.form.get("temperature_c")

    if not water_level:
        flash("Water level is required.")
        return redirect(url_for("monitoring.index"))

    new_reading = EnvironmentalReading(
        source="manual",
        location_label=request.form.get("location_label") or "Central Bay, Cardona, Rizal",
        water_level_m=float(water_level),
        wave_height_m=float(wave_height) if wave_height else None,
        wind_speed_kmh=float(wind_speed) if wind_speed else None,
        wind_direction=wind_direction or None,
        weather_condition=weather_condition or None,
        temperature_c=float(temperature) if temperature else None,
        entered_by_user_id=session.get("user_id"),
        recorded_at=datetime.now(timezone.utc)
)
    db.session.add(new_reading)
    db.session.commit()

    flash("Environmental reading recorded successfully.")
    return redirect(url_for("monitoring.index"))



