from flask import render_template, request, redirect, url_for, flash
from app.blueprints.monitoring import monitoring_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading



@monitoring_bp.route("/monitoring")
@login_required
def index():
    latest = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).first()

    history = EnvironmentalReading.query.order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).limit(10).all()

    return render_template(
        "monitoring/index.html",
        latest=latest,
        history=history,
        active_page="monitoring"
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
        water_level_m=float(water_level),
        wave_height_m=float(wave_height) if wave_height else None,
        wind_speed_kmh=float(wind_speed) if wind_speed else None,
        wind_direction=wind_direction or None,
        weather_condition=weather_condition or None,
        temperature_c=float(temperature) if temperature else None,
        entered_by_user_id=session.get("user_id")
    )
    db.session.add(new_reading)
    db.session.commit()

    flash("Environmental reading recorded successfully.")
    return redirect(url_for("monitoring.index"))

