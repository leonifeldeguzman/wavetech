from datetime import date, datetime
from flask import render_template, request, redirect, url_for
from app.blueprints.manifests import manifests_bp
from app.utils.decorators import login_required
from app.extensions import db
from app.models.trip import Trip
from app.models.boat import Boat
from app.models.pending_registration import PendingRegistration
from app.models.manifest_entry import ManifestEntry
from flask import render_template, request, redirect, url_for, flash

@manifests_bp.route("/manifests")
@login_required
def list_slots():
    today = date.today()

    slots = Trip.query.filter(
        db.func.date(Trip.departure_time) == today
    ).order_by(Trip.departure_time).all()

    return render_template("manifests/list.html", slots=slots)


@manifests_bp.route("/manifests/add-slot", methods=["POST"])
@login_required
def add_slot():
    departure_time_str = request.form.get("departure_time")
    today = date.today()

    departure_time = datetime.combine(
        today, datetime.strptime(departure_time_str, "%H:%M").time()
    )

    new_trip = Trip(
        departure_time=departure_time,
        status="Open"
    )
    db.session.add(new_trip)
    db.session.commit()

    return redirect(url_for("manifests.list_slots"))

@manifests_bp.route("/manifests/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    pending = PendingRegistration.query.filter_by(
        trip_id=trip.id, status="pending"
    ).order_by(PendingRegistration.submitted_at).all()

    manifest = ManifestEntry.query.filter_by(
        trip_id=trip.id
    ).order_by(ManifestEntry.check_in_time).all()

    boats = Boat.query.all()

    current_count = len(manifest)
    capacity = trip.boat.capacity if trip.boat else None

    return render_template(
        "manifests/detail.html",
        trip=trip,
        pending=pending,
        manifest=manifest,
        boats=boats,
        current_count=current_count,
        capacity=capacity
    )


@manifests_bp.route("/manifests/<int:trip_id>/assign-boat", methods=["POST"])
@login_required
def assign_boat(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    boat_id = request.form.get("boat_id")
    crew_name = request.form.get("crew_name")

    trip.boat_id = boat_id if boat_id else None
    trip.crew_name = crew_name if crew_name else None
    db.session.commit()

    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

@manifests_bp.route("/manifests/<int:trip_id>/add-walkin", methods=["POST"])
@login_required
def add_walkin(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    if not trip.boat:
        flash("Cannot register passengers — no boat assigned to this trip yet.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    current_count = ManifestEntry.query.filter_by(trip_id=trip.id).count()

    if current_count >= trip.boat.capacity:
        trip.status = "Full"
        db.session.commit()
        flash("This trip is at full capacity. No additional passengers can be registered.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    full_name = request.form.get("full_name")
    age = request.form.get("age")
    address = request.form.get("address")
    contact_number = request.form.get("contact_number")
    passenger_type = request.form.get("passenger_type")

    new_entry = ManifestEntry(
        trip_id=trip.id,
        full_name=full_name,
        age=int(age),
        address=address,
        contact_number=contact_number,
        passenger_type=passenger_type,
        source="walkin"
    )
    db.session.add(new_entry)
    db.session.commit()

    new_count = current_count + 1
    if new_count >= trip.boat.capacity:
        trip.status = "Full"
        db.session.commit()

    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

@manifests_bp.route("/manifests/<int:trip_id>/delay", methods=["POST"])
@login_required
def delay_trip(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    reason = request.form.get("delay_reason")
    new_time_str = request.form.get("new_departure_time")

    if not reason or not new_time_str:
        flash("Please provide a reason and new departure time.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    today = trip.departure_time.date()
    new_time = datetime.combine(
        today, datetime.strptime(new_time_str, "%H:%M").time()
    )

    trip.status = "Delayed"
    trip.delay_reason = reason
    trip.new_departure_time = new_time
    db.session.commit()

    flash(f"Trip delayed to {new_time.strftime('%I:%M %p')}. Reason: {reason}")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/cancel", methods=["POST"])
@login_required
def cancel_trip(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    reason = request.form.get("cancel_reason")

    if not reason:
        flash("Please provide a reason for cancellation.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    trip.status = "Cancelled"
    trip.cancel_reason = reason
    db.session.commit()

    flash(f"Trip cancelled. Reason: {reason}")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

@manifests_bp.route("/manifests/<int:trip_id>/pending/<int:reg_id>/approve", methods=["POST"])
@login_required
def approve_registration(trip_id, reg_id):
    trip = Trip.query.get_or_404(trip_id)
    registration = PendingRegistration.query.get_or_404(reg_id)

    if not trip.boat:
        flash("Cannot approve — no boat assigned to this trip yet.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    current_count = ManifestEntry.query.filter_by(trip_id=trip.id).count()

    if current_count >= trip.boat.capacity:
        trip.status = "Full"
        db.session.commit()
        flash("Cannot approve — trip is at full capacity.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    new_entry = ManifestEntry(
        trip_id=trip.id,
        full_name=registration.full_name,
        age=registration.age,
        address=registration.address,
        contact_number=registration.contact_number,
        passenger_type=registration.passenger_type,
        source="online"
    )
    db.session.add(new_entry)

    registration.status = "approved"

    db.session.commit()

    new_count = current_count + 1
    if new_count >= trip.boat.capacity:
        trip.status = "Full"
        db.session.commit()

    flash(f"{registration.full_name} approved and added to manifest.")
    return redirect(url_for("manifests.trip_detail", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/pending/<int:reg_id>/reject", methods=["POST"])
@login_required
def reject_registration(trip_id, reg_id):
    registration = PendingRegistration.query.get_or_404(reg_id)
    registration.status = "rejected"
    db.session.commit()

    flash(f"{registration.full_name}'s registration was rejected.")
    return redirect(url_for("manifests.trip_detail", trip_id=trip_id))

@manifests_bp.route("/manifests/<int:trip_id>/confirm-departure", methods=["POST"])
@login_required
def confirm_departure(trip_id):
    trip = Trip.query.get_or_404(trip_id)

    audit_confirmed = request.form.get("audit_confirmed")

    if not audit_confirmed:
        flash("You must confirm the Coast Guard audit before departure.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    if trip.status in ["Cancelled", "Departed"]:
        flash("This trip cannot be departed — it is already Cancelled or Departed.")
        return redirect(url_for("manifests.trip_detail", trip_id=trip.id))

    trip.coast_guard_audit_confirmed = True
    trip.status = "Departed"
    trip.departed_at = db.func.now()
    db.session.commit()

    return redirect(url_for("manifests.departure_success", trip_id=trip.id))


@manifests_bp.route("/manifests/<int:trip_id>/departure-success")
@login_required
def departure_success(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    return render_template("manifests/departure_success.html", trip=trip)