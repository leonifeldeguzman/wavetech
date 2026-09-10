from flask import flash, redirect, render_template, request, session, url_for

from app.blueprints.settings import settings_bp
from app.extensions import db
from app.models.user import User
from app.services import activity_log_service, settings_service
from app.services.settings_service import SettingsValidationError
from app.utils.decorators import admin_required, login_required
from app.utils.security import hash_password, verify_password

ROLE_ADMIN = "Admin"


@settings_bp.route("/settings")
@login_required
def index():
    user = db.session.get(User, session["user_id"])
    is_admin = session.get("role") == ROLE_ADMIN

    thresholds = settings_service.get_safety_thresholds()
    refresh_interval = settings_service.get_refresh_interval_seconds()
    session_timeout = settings_service.get_session_timeout_minutes()

    activity_logs = activity_log_service.get_recent_logs(limit=25) if is_admin else []

    return render_template(
        "settings/index.html",
        user=user,
        is_admin=is_admin,
        thresholds=thresholds,
        refresh_interval=refresh_interval,
        refresh_interval_options=settings_service.REFRESH_INTERVAL_OPTIONS,
        session_timeout=session_timeout,
        activity_logs=activity_logs,
        active_page="settings",
    )


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/change-password", methods=["POST"])
@login_required
def change_password():
    user = db.session.get(User, session["user_id"])

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if user is None or not verify_password(current_password, user.password_hash):
        flash("Current password is incorrect.")
        return redirect(url_for("settings.index"))

    if len(new_password) < 8:
        flash("New password must be at least 8 characters long.")
        return redirect(url_for("settings.index"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.")
        return redirect(url_for("settings.index"))

    user.password_hash = hash_password(new_password)
    db.session.commit()

    flash("Password updated successfully.")
    return redirect(url_for("settings.index"))


# ---------------------------------------------------------------------------
# Safety Thresholds (Admin only)
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/safety-thresholds", methods=["POST"])
@admin_required
def update_safety_thresholds():
    form = request.form
    try:
        changes = settings_service.update_safety_thresholds(
            wind_safe_max_kmh=form.get("wind_safe_max_kmh"),
            wind_caution_max_kmh=form.get("wind_caution_max_kmh"),
            water_safe_min_m=form.get("water_safe_min_m"),
            water_safe_max_m=form.get("water_safe_max_m"),
            water_caution_min_m=form.get("water_caution_min_m"),
            water_caution_max_m=form.get("water_caution_max_m"),
            weather_safe=form.get("weather_safe"),
            weather_caution=form.get("weather_caution"),
            weather_unsafe=form.get("weather_unsafe"),
            updated_by_user_id=session.get("user_id"),
        )
    except SettingsValidationError as exc:
        flash(str(exc))
        return redirect(url_for("settings.index"))

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_UPDATED_SAFETY_THRESHOLDS,
        details=changes,
    )

    flash("Safety thresholds updated successfully.")
    return redirect(url_for("settings.index"))


# ---------------------------------------------------------------------------
# Refresh Interval
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/refresh-interval", methods=["POST"])
@login_required
def update_refresh_interval():
    try:
        seconds = settings_service.update_refresh_interval(
            request.form.get("refresh_interval_seconds")
        )
    except SettingsValidationError as exc:
        flash(str(exc))
        return redirect(url_for("settings.index"))

    flash(f"Refresh interval set to {seconds} seconds.")
    return redirect(url_for("settings.index"))


# ---------------------------------------------------------------------------
# Security & Activity (Admin only)
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/session-timeout", methods=["POST"])
@admin_required
def update_session_timeout():
    try:
        minutes = settings_service.update_session_timeout(
            request.form.get("session_timeout_minutes")
        )
    except SettingsValidationError as exc:
        flash(str(exc))
        return redirect(url_for("settings.index"))

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_UPDATED_SESSION_SETTINGS,
        details=f"Session timeout set to {minutes} minutes.",
    )

    flash(f"Session timeout set to {minutes} minutes.")
    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/activity-log")
@admin_required
def activity_log():
    logs = activity_log_service.get_recent_logs(limit=200)
    return render_template("settings/activity_log.html", activity_logs=logs, active_page="settings")
