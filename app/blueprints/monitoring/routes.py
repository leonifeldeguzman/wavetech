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
from app.services import historical_baseline_service, monitoring_service, settings_service
from app.utils.ph_clock import ph_today
from app.utils.decorators import login_required
from datetime import datetime, timedelta, timezone

# A recorded water level older than this is not compared with the baseline.
BASELINE_COMPARE_MAX_AGE = timedelta(hours=24)


@monitoring_bp.route("/monitoring")
@login_required
def index():
    # Get current environmental conditions
    llda_conditions = monitoring_service.get_llda_conditions()
    station_weather = monitoring_service.get_station_weather()

        # Rows from the per-station weather proxy and the old (test-key) Windy
    # rows are not shown here; station weather has its own section.
    hidden_sources = (monitoring_service.SOURCE_OPEN_METEO, "windy")

    latest = monitoring_service.get_latest_reading()

    history = EnvironmentalReading.query.filter(
        EnvironmentalReading.source.notin_(hidden_sources)
    ).order_by(
        EnvironmentalReading.retrieved_at.desc()
    ).limit(10).all()
   

        # Historical baseline: seasonal context only, never a current reading.
    baseline = historical_baseline_service.get_baseline_for_day(ph_today())

    # Newest water level from a real current source (LLDA or manual).
    # Not `latest` above: that is the newest row of ANY source, which is
    # often a Windy row with no water level.
    baseline_water_reading = monitoring_service.get_latest_water_reading()

    baseline_context = None
    if baseline and baseline_water_reading:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        age = now_utc - baseline_water_reading.retrieved_at
        # Compare only a recent reading, and reject a negative age.
        if timedelta(0) <= age <= BASELINE_COMPARE_MAX_AGE:
            baseline_context = historical_baseline_service.classify_level(
                baseline_water_reading.water_level_m, baseline
            )

    # Water level for the top card: newest LLDA/manual level, shown with its
    # age. Display only; it does not feed any recommendation.
    water_level_card = monitoring_service.build_water_level_card(
        baseline_water_reading
    )

        # Per-station weather cards. Stations with identical model values fall
    # in the same grid cell, so we tell the operator which ones share one.
    def _values(reading):
        return (reading.wind_speed_kmh, reading.wind_direction,
                reading.temperature_c, reading.weather_condition)

    groups = {}
    for item in station_weather["stations"]:
        if item["reading"] is not None:
            groups.setdefault(_values(item["reading"]), []).append(
                item["station"].station_no
            )

    station_cards = []
    for item in station_weather["stations"]:
        reading = item["reading"]
        shared = []
        if reading is not None:
            shared = [n for n in groups[_values(reading)]
                      if n != item["station"].station_no]
        station_cards.append({
            "station": item["station"],
            "reading": reading,
            "shared_with": shared,
        })

    station_weather_updated = next(
        (c["reading"].retrieved_at for c in station_cards if c["reading"]),
        None,
    )
    
    _severity = {"Rainy": 3, "Cloudy": 2, "Sunny": 1}
    with_data = [c for c in station_cards if c["reading"] is not None]
    weather_summary = None
    wind_summary = None
    if with_data:
        worst = max((c["reading"].weather_condition for c in with_data),
                    key=lambda w: _severity.get(w, 0))
        temps = [c["reading"].temperature_c for c in with_data
                 if c["reading"].temperature_c is not None]
        weather_summary = {
            "condition": worst,
            "count": sum(1 for c in with_data
                         if c["reading"].weather_condition == worst),
            "total": len(with_data),
            "temp_min": min(temps) if temps else None,
            "temp_max": max(temps) if temps else None,
        }
        winds = [c for c in with_data if c["reading"].wind_speed_kmh is not None]
        if winds:
            top = max(winds, key=lambda c: c["reading"].wind_speed_kmh)
            wind_summary = {
                "speed": top["reading"].wind_speed_kmh,
                "direction": top["reading"].wind_direction,
                "station_name": top["station"].name,
                "station_no": top["station"].station_no,
            }
    

    map_stations = [
        {
            "no": c["station"].station_no,
            "name": c["station"].name,
            "lat": c["station"].latitude,
            "lon": c["station"].longitude,
            "weather": c["reading"].weather_condition if c["reading"] else None,
            "temp": c["reading"].temperature_c if c["reading"] else None,
            "wind": c["reading"].wind_speed_kmh if c["reading"] else None,
            "dir": c["reading"].wind_direction if c["reading"] else None,
        }
        for c in station_cards
    ]

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

        weather_summary=weather_summary,
        wind_summary=wind_summary,

       
        station_cards=station_cards,
        station_weather_status=station_weather["status"],
        station_weather_updated=station_weather_updated,
        map_stations=map_stations,

        

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

        baseline=baseline,
        baseline_water_reading=baseline_water_reading,
        baseline_context=baseline_context,
        water_level_card=water_level_card,
    )


@monitoring_bp.route("/monitoring/add-reading", methods=["POST"])
@login_required
def add_reading():
    # Get values from manual entry form
    water_level = request.form.get("water_level_m")
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