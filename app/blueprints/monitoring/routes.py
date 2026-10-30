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
from datetime import date, datetime, timedelta, timezone
from app.models.lake_station import LakeStation

# A recorded water level older than this is not compared with the baseline.
BASELINE_COMPARE_MAX_AGE = timedelta(hours=24)

def _parse_date(value):
    """A YYYY-MM-DD query value as a date, or None if missing/invalid."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


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

    # Condition for that same reading, from the existing classifier that
    # reads the Safety Thresholds saved in Admin Settings (shared with the
    # dashboard, passenger home and decision-support). With no reading
    # there is no condition: it is never guessed.
    water_safety_status = None
    water_safety_label = None
    if water_level_card["reading"] is not None:
        water_safety_status, water_safety_label = (
            monitoring_service.classify_water_safety(
                water_level_card["reading"].water_level_m
            )
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
    
    with_data = [c for c in station_cards if c["reading"] is not None]
    weather_summary = None
    wind_summary = None

    # Headline Weather = the reference station's observation, the same
    # reading the Passenger home page and the Dashboard assessment use.
    # (It used to be the "worst" condition across all stations, which can
    # differ from the reference station.) Other stations stay visible in
    # the station cards and on the map.
    ref = monitoring_service.pick_reference_reading(station_weather)
    if ref is not None:
        ref_station = next(
            c["station"] for c in station_cards
            if c["station"].station_no == monitoring_service.DSS_STATION_NO
        )
        weather_summary = {
            "condition": ref.weather_condition,
            "station_name": ref_station.name,
            "station_no": ref_station.station_no,
            "temperature_c": ref.temperature_c,
        }
    if with_data:
        winds = [c for c in with_data if c["reading"].wind_speed_kmh is not None]
        if winds:
            top = max(winds, key=lambda c: c["reading"].wind_speed_kmh)
            wind_summary = {
                "speed": top["reading"].wind_speed_kmh,
                "direction": top["reading"].wind_direction,
                "station_name": top["station"].name,
                "station_no": top["station"].station_no,
            }

    # Weather as decision-support sees it (one shared rule). When the model
    # is not live and a recent manual entry stands in, the top cards show
    # that entry labeled as manual, never as weather-model data.
    latest_weather = monitoring_service.get_dss_weather_conditions(station_weather)

    manual_wx = (
        latest_weather["reading"]
        if latest_weather["status"] == monitoring_service.STATUS_MANUAL
        else None
    )
    if manual_wx is not None:
        manual_station = next(
            (c["station"] for c in station_cards
             if c["station"].station_no == monitoring_service.DSS_STATION_NO),
            None,
        )
        if manual_station is not None:
            weather_summary = {
                "condition": manual_wx.weather_condition,
                "station_name": manual_station.name,
                "station_no": manual_station.station_no,
                "temperature_c": manual_wx.temperature_c,
                "source": "manual",
                "retrieved_at": manual_wx.retrieved_at,
            }
            wind_summary = {
                "speed": manual_wx.wind_speed_kmh,
                "direction": manual_wx.wind_direction,
                "station_name": manual_station.name,
                "station_no": manual_station.station_no,
                "source": "manual",
                "retrieved_at": manual_wx.retrieved_at,
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

    # Lake-wide water-level history for the Trends chart (display only).
    trend = historical_baseline_service.get_water_level_history(
            _parse_date(request.args.get("trend_start")),
            _parse_date(request.args.get("trend_end")),
    )

    # Per-station weather history for the Station Weather Trend (model data).
    station_trend = monitoring_service.get_station_history(
        request.args.get("station"),
        _parse_date(request.args.get("wx_start")),
        _parse_date(request.args.get("wx_end")),
    )

       

    # Manual weather form: the dropdown lists exactly the conditions saved in
    # Admin Settings, so every choice can be classified by the rules.
    weather_options = sorted({
        label.title()
        for category in settings_service.get_weather_categories().values()
        for label in category
    })
    dss_station = LakeStation.query.filter_by(
        station_no=monitoring_service.DSS_STATION_NO
    ).first()
    
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
        trend=trend,
        station_trend=station_trend,
        baseline_water_reading=baseline_water_reading,
        baseline_context=baseline_context,
        water_level_card=water_level_card,
        water_safety_status=water_safety_status,
        water_safety_label=water_safety_label,
        weather_options=weather_options,
        dss_station=dss_station,
        latest_weather=latest_weather,
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
    observed_condition = (request.form.get("observed_condition") or "").strip().lower()
    observer_comment = (request.form.get("observer_comment") or "").strip()

    # Only the three known values are accepted ("" means not observed).
    if observed_condition and observed_condition not in EnvironmentalReading.OBSERVED_CONDITIONS:
        flash("Invalid water condition selected.")
        return redirect(url_for("monitoring.index"))

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
        observed_condition=observed_condition or None,
        observer_comment=observer_comment[:300] or None,
    )

    

    db.session.add(new_reading)
    db.session.commit()

    flash("Environmental reading recorded successfully.", "success")

    return redirect(url_for("monitoring.index"))

@monitoring_bp.route("/monitoring/add-weather", methods=["POST"])
@login_required
def add_weather_reading():
    """Manual weather for the reference station only (Cardona, XXI). Saved
    as a NEW manual row; Open-Meteo rows are never edited. It stands in for
    the weather model only while that is cached or unavailable."""
    station = LakeStation.query.filter_by(
        station_no=monitoring_service.DSS_STATION_NO
    ).first()
    if station is None:
        flash("The reference station is not configured.")
        return redirect(url_for("monitoring.index"))

    wind_speed = (request.form.get("wind_speed_kmh") or "").strip()
    condition = (request.form.get("weather_condition") or "").strip()
    direction = (request.form.get("wind_direction") or "").strip()
    temperature = (request.form.get("temperature_c") or "").strip()

    if not wind_speed or not condition:
        flash("Wind speed and weather condition are required.")
        return redirect(url_for("monitoring.index"))

    # Only conditions saved in Admin Settings are accepted.
    allowed = {
        label
        for category in settings_service.get_weather_categories().values()
        for label in category
    }
    if condition.lower() not in allowed:
        flash("Invalid weather condition selected.")
        return redirect(url_for("monitoring.index"))

    try:
        wind_value = float(wind_speed)
        temp_value = float(temperature) if temperature else None
    except ValueError:
        flash("Wind speed and temperature must be numbers.")
        return redirect(url_for("monitoring.index"))
    if wind_value < 0:
        flash("Wind speed cannot be negative.")
        return redirect(url_for("monitoring.index"))

    db.session.add(EnvironmentalReading(
        source="manual",
        station_id=station.id,
        location_label=f"{station.name} (Station {station.station_no})",
        wind_speed_kmh=wind_value,
        wind_direction=direction[:10] or None,
        weather_condition=condition.title(),
        temperature_c=temp_value,
        entered_by_user_id=session.get("user_id"),
    ))
    db.session.commit()

    flash("Manual weather recorded for the reference station.", "success")
    return redirect(url_for("monitoring.index"))