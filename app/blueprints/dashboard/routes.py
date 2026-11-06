from datetime import datetime, timedelta
from app.utils.ph_clock import ph_now, ph_today
from flask import current_app, jsonify, render_template, request
from app.blueprints.dashboard import dashboard_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.trip import Trip
from app.models.manifest_entry import ManifestEntry
from app.models.environmental_reading import EnvironmentalReading
from app.services import historical_baseline_service, manifest_service, monitoring_service, scheduling_service, settings_service, trip_state_service


# Only a trip that is fully assigned (boat, operator, crew) and still live can
# be assessed. "Unassigned" slots are left out until they are assigned.
ASSESSABLE_STATUSES = ("Open", "Full", "Delayed")


@dashboard_bp.route("/dashboard")
@login_required
def index():
    today = ph_today()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    all_trips_today = Trip.query.filter(
        Trip.departure_time >= day_start,
        Trip.departure_time < day_end,
    ).order_by(Trip.departure_time).all()

    # Weather cannot be forecast, so only today's remaining trips can be
    # assessed (never a trip on a future date).
    scheduled_trips = Trip.query.filter(
        Trip.departure_time >= ph_now(),
        Trip.departure_time < day_end,
        Trip.status.notin_(["Cancelled", "Departed"]),
    ).order_by(Trip.departure_time).all()

    live_statuses = ("Open", "Boarding", "Full", "Delayed")
    now = ph_now()
    # Same rule as the Manifests list (trip_state_service.is_expired), so the
    # Expired badge can never differ between the two pages.
    expired_ids = {
        t.id for t in all_trips_today
        if trip_state_service.is_expired(t, now=now)
    }
    active_trips_count = sum(
        1 for t in all_trips_today
        if t.status in live_statuses and t.id not in expired_ids
    )

    # Real status per trip (same rule as the Manifests list): "Unassigned"
    # until boat, operator and crew are all assigned.
    # "Full" is derived from the live seat count, same as the timetable.
    # Confirmed passengers only (manifest entries), exactly as the Manifests
    # list and the passenger portal count them. Pending registrations do not
    # hold a seat, so they must not push a trip to "Full" here.
    seat_counts = {t.id: 0 for t in all_trips_today}
    if seat_counts:
        rows = (
            db.session.query(ManifestEntry.trip_id, db.func.count(ManifestEntry.id))
            .filter(ManifestEntry.trip_id.in_(list(seat_counts)))
            .group_by(ManifestEntry.trip_id)
            .all()
        )
        seat_counts.update(dict(rows))
    display_status = {
        t.id: manifest_service.effective_status(t, seat_counts[t.id])
        for t in all_trips_today
    }

    # The assessment dropdown lists Open, Full and Delayed trips only, so an
    # Unassigned slot appears once boat, operator and crew are all saved.
    scheduled_trips = [
        t for t in scheduled_trips
        if display_status.get(t.id) in ASSESSABLE_STATUSES
    ]

    trip_ids_today = [t.id for t in all_trips_today]
    passengers_today = 0
    if trip_ids_today:
        passengers_today = ManifestEntry.query.filter(
            ManifestEntry.trip_id.in_(trip_ids_today)
        ).count()

    departed_trips_count = Trip.query.filter(
        Trip.status == "Departed",
        Trip.departure_time >= day_start,
        Trip.departure_time < day_end,
    ).count()
    

    # One assessment feeds both the Safety Status card and Decision-Support
    # below, so the two always show the same condition.
    assessment = scheduling_service.get_assessment()
    safety_status, safety_label, latest_reading = monitoring_service.get_current_safety(
        assessment=assessment
    )

    # Scheduling Decision-Support is read-only. The selected trip is used
    # only as context for the operator; no Trip or manifest row is changed.
    selected_trip = None
    scheduling_assessment = None
    safety_alert_action = None
    level_context = None
    trip_id = request.args.get("trip_id", type=int)
    if trip_id is not None:
        selected_trip = db.session.get(Trip, trip_id)
        # A hand-edited URL for another day gets no assessment.
        if selected_trip is not None and selected_trip.departure_time.date() != today:
            selected_trip = None
        # Same for a hand-edited URL naming an Unassigned (or otherwise
        # non-assessable) trip: it is not in the dropdown, so no assessment.
        if selected_trip is not None and display_status.get(selected_trip.id) not in ASSESSABLE_STATUSES:
            selected_trip = None
        # A departed or expired trip is a closed record and can no longer be
        # assessed (hand-edited URL).
        if selected_trip is not None and trip_state_service.is_read_only(selected_trip, now=ph_now()):
            selected_trip = None
        if selected_trip is not None:
            scheduling_assessment = assessment
            # Only offered for DELAY and DO NOT DEPART results — see
            # scheduling_service.build_safety_alert(). This never creates
            # an Announcement itself; it only supplies the text used to
            # prefill the existing Create Notice form.
            safety_alert_action = scheduling_service.build_safety_alert(
                scheduling_assessment, selected_trip
            )
            level_context = historical_baseline_service.get_level_context()

            


    return render_template(
        "dashboard/index.html",
        trips=all_trips_today,
        expired_ids=expired_ids,
        display_status=display_status,
        scheduled_trips=scheduled_trips,
        active_trips_count=active_trips_count,
        passengers_today=passengers_today,
        departed_trips_count=departed_trips_count,
        safety_status=safety_status,
        safety_label=safety_label,
        latest_reading=latest_reading,
        selected_trip=selected_trip,
        scheduling_assessment=scheduling_assessment,
        safety_alert_action=safety_alert_action,
        level_context=level_context,
        refresh_interval_seconds=settings_service.get_refresh_interval_seconds(),
        active_page="dashboard"
    )


@dashboard_bp.route("/dashboard/overall-condition")
@login_required
def overall_condition():
    """Overall Condition summary as JSON, for the Manifesting page indicator.

    Same evaluation as the Dashboard (scheduling_service.get_assessment()),
    so the two cannot disagree. Read-only; no-store so a browser never shows
    a cached condition.
    """
    condition = scheduling_service.get_current_overall_condition()
    response = jsonify({
        "status": condition["status"],
        "label": condition["label"],
        "css_class": condition["css_class"],
        "badge": condition["badge"],
        "summary": condition["summary"],
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@dashboard_bp.route("/dashboard/dss-details/<int:trip_id>")
@login_required
def dss_details(trip_id):
    """The Dashboard's Decision-Support details card, as an HTML fragment.

    Used by the Manifesting "Assess Condition" button. It runs the same
    scheduling_service.get_assessment() and renders the same
    partials/dss_details_modal.html the Dashboard "View Details" uses, so
    there is one assessment and one result card. Read-only: no Trip or
    manifest row is changed.
    """
    trip = db.session.get(Trip, trip_id)
    if trip is None:
        response = jsonify({"error": "Trip not found."})
        response.status_code = 404
        response.headers["Cache-Control"] = "no-store"
        return response

    # A departed trip is a closed record: no further condition assessment.
    if trip_state_service.is_departed(trip):
        response = jsonify({"error": "This trip has departed."})
        response.status_code = 409
        response.headers["Cache-Control"] = "no-store"
        return response

    # An expired trip is closed as well.
    if trip_state_service.is_expired(trip, now=ph_now()):
        response = jsonify({"error": "This trip has expired."})
        response.status_code = 409
        response.headers["Cache-Control"] = "no-store"
        return response

    # An Unassigned slot (no boat, operator or crew yet) cannot be assessed.
    if manifest_service.effective_status(trip) == "Unassigned":
        response = jsonify({
            "error": "Assign the boat, operator and crew before assessing conditions."
        })
        response.status_code = 409
        response.headers["Cache-Control"] = "no-store"
        return response

    # Weather cannot be forecast: assess only trips scheduled for today (PH).
    if trip.departure_time.date() != ph_today():
        response = jsonify({
            "error": "Conditions can only be assessed on the day of the trip."
        })
        response.status_code = 409
        response.headers["Cache-Control"] = "no-store"
        return response

    try:
        assessment = scheduling_service.get_assessment()
        html = render_template(
            "partials/dss_details_modal.html",
            selected_trip=trip,
            scheduling_assessment=assessment,
        )
    except Exception:
        current_app.logger.exception("DSS details failed for trip %s", trip_id)
        response = jsonify({"error": "The assessment could not be retrieved."})
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    response = current_app.response_class(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response