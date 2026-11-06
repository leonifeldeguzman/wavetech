import json

from flask import Response, abort, flash, redirect, render_template, request, session, url_for

from app.blueprints.settings import settings_bp
from app.extensions import db
from app.models.user import User
from app.services import (
    activity_log_service,
    observation_rules_service,
    profile_picture_service,
    settings_service,
)
from app.services.observation_rules_service import ObservationRuleError
from app.services.profile_picture_service import ProfilePictureError
from app.services.settings_service import SettingsValidationError
from app.utils.decorators import login_required
from app.utils.security import hash_password, verify_password

def _back(section: str):
    """Return to Settings on the same tab the form was on. Without this the
    page reloads on My Profile, so a save (or its error message) in another
    tab looks like it did nothing."""
    return redirect(url_for("settings.index", section=section))


@settings_bp.route("/settings")
@login_required
def index():
    user = db.session.get(User, session["user_id"])
    # Admin and Operator accounts share identical Settings access (Account,
    # Safety Thresholds, Refresh Interval, and Security & Activity) — see
    # app/utils/decorators.py, which no longer distinguishes roles for any
    # of these routes. `is_admin` is kept as a template variable (rather
    # than removing the templates' conditionals) so nothing needs to
    # change if a genuinely Admin-only setting is introduced later.
    is_admin = True

    thresholds = settings_service.get_safety_thresholds()
    refresh_interval = settings_service.get_refresh_interval_seconds()
    session_timeout = settings_service.get_session_timeout_minutes()

    activity_logs = activity_log_service.get_recent_logs(limit=25)

    return render_template(
        "settings/index.html",
        user=user,
        is_admin=is_admin,
        thresholds=thresholds,
        refresh_interval=refresh_interval,
        refresh_interval_options=settings_service.REFRESH_INTERVAL_OPTIONS,
        session_timeout=session_timeout,
        observation_rules=observation_rules_service.list_rules(),
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
        flash("Current password is incorrect.", "danger")
        return _back("account-section")

    if len(new_password) < 8:
        flash("New password must be at least 8 characters long.", "danger")
        return _back("account-section")

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "danger")
        return _back("account-section")

    user.password_hash = hash_password(new_password)
    db.session.commit()

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_CHANGED_PASSWORD,
        details="Password changed successfully.",
    )

    flash("Password updated successfully.", "success")
    return _back("account-section")

@settings_bp.route("/settings/update-display-name", methods=["POST"])
@login_required
def update_display_name():
    user = db.session.get(User, session["user_id"])

    if user is None:
        flash("User account could not be found.", "danger")
        return _back("account-section")

    display_name = request.form.get("display_name", "").strip()

    if not display_name:
        flash("Display name cannot be empty.", "danger")
        return _back("account-section")

    if len(display_name) > 100:
        flash("Display name must be 100 characters or less.", "danger")
        return _back("account-section")

    user.full_name = display_name

    # Update the session so the header immediately shows the new name
    session["full_name"] = display_name

    db.session.commit()

    flash("Display name updated successfully.", "success")
    return _back("account-section")


@settings_bp.route("/settings/profile-picture")
@login_required
def profile_picture():
    """Serve the logged-in user's own profile picture (404 if none)."""
    picture = profile_picture_service.get_picture(session["user_id"])
    if picture is None:
        abort(404)

    response = Response(picture.image_data, mimetype=picture.content_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # The URL carries ?v=<updated_at>, so a new upload gets a new URL.
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@settings_bp.route("/settings/profile-picture/upload", methods=["POST"])
@login_required
def upload_profile_picture():
    """Upload a new picture, or replace the existing one."""
    user = db.session.get(User, session["user_id"])
    if user is None:
        flash("User account could not be found.", "danger")
        return _back("account-section")

    uploaded = request.files.get("profile_picture")
    if not uploaded or not uploaded.filename:
        flash("Please choose an image to upload.", "danger")
        return _back("account-section")

    try:
        data, content_type = profile_picture_service.read_and_validate(uploaded)
    except ProfilePictureError as exc:
        flash(str(exc), "danger")
        return _back("account-section")

    profile_picture_service.save_picture(user.id, data, content_type)
    db.session.commit()

    flash("Profile picture updated successfully.", "success")
    return _back("account-section")


@settings_bp.route("/settings/profile-picture/remove", methods=["POST"])
@login_required
def remove_profile_picture():
    """Remove the picture; the default first-letter avatar is shown again."""
    removed = profile_picture_service.delete_picture(session["user_id"])
    db.session.commit()

    if removed:
        flash("Profile picture removed.", "success")
    else:
        flash("There is no profile picture to remove.", "warning")
    return _back("account-section")

@settings_bp.route("/settings/update-personal-info", methods=["POST"])
@login_required
def update_personal_info():
    user = db.session.get(User, session["user_id"])

    if user is None:
        flash("User account could not be found.", "danger")
        return _back("account-section")

    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    phone = request.form.get("phone", "").strip()

    if not first_name:
        flash("First name cannot be empty.", "danger")
        return _back("account-section")

    if not last_name:
        flash("Last name cannot be empty.", "danger")
        return _back("account-section")

    if len(first_name) > 50:
        flash("First name must be 50 characters or less.", "danger")
        return _back("account-section")

    if len(last_name) > 50:
        flash("Last name must be 50 characters or less.", "danger")
        return _back("account-section")

    full_name = f"{first_name} {last_name}"

    user.full_name = full_name
    session["full_name"] = full_name
    user.phone = phone

    db.session.commit()

    flash("Personal information updated successfully.", "success")
    return _back("account-section")
    
# ---------------------------------------------------------------------------
# Safety Thresholds (shared Admin/Operator access)
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/safety-thresholds", methods=["POST"])
@login_required
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
        flash(str(exc), "danger")
        return _back("safety-section")

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_UPDATED_SAFETY_THRESHOLDS,
        details=changes,
    )

    flash("Safety thresholds updated successfully.", "success")
    return _back("safety-section")


# ---------------------------------------------------------------------------
# Operator Observation Rules (Admin can add / edit / delete)
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/observation-rules", methods=["POST"])
@login_required
def update_observation_rules():
    try:
        items = json.loads(request.form.get("observation_rules_json", "[]"))
        changes = observation_rules_service.save_rules(
            items, updated_by_user_id=session.get("user_id")
        )
    except (ValueError, TypeError, AttributeError) as exc:
        # json errors and ObservationRuleError are both ValueError
        message = str(exc) if isinstance(exc, ObservationRuleError) else "Invalid observation data."
        flash(message, "danger")
        return _back("safety-section")

    if changes:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            # Add ACTION_UPDATED_OBSERVATION_RULES to activity_log_service to
            # give it its own label; until then it is logged as a threshold change.
            action=getattr(
                activity_log_service,
                "ACTION_UPDATED_OBSERVATION_RULES",
                activity_log_service.ACTION_UPDATED_SAFETY_THRESHOLDS,
            ),
            details=" ".join(changes),
        )
        flash("Operator observation rules updated successfully.", "success")
    else:
        flash("No changes to save.", "warning")

    return _back("safety-section")


# ---------------------------------------------------------------------------
# Refresh Interval
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/refresh-interval", methods=["POST"])
@login_required
def update_refresh_interval():
    old_seconds = settings_service.get_refresh_interval_seconds()

    try:
        seconds = settings_service.update_refresh_interval(
            request.form.get("refresh_interval_seconds")
        )
    except SettingsValidationError as exc:
        flash(str(exc), "danger")
        return _back("refresh-section")

    if old_seconds != seconds:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_UPDATED_REFRESH_INTERVAL,
            details=f"Refresh interval: {old_seconds} -> {seconds} seconds.",
        )

    if seconds == 0:
        flash("Auto-refresh turned off.", "success")
    else:
        flash(f"Refresh interval set to {seconds} seconds.", "success")

    return _back("refresh-section")


# ---------------------------------------------------------------------------
# Security & Activity (shared Admin/Operator access)
# ---------------------------------------------------------------------------

@settings_bp.route("/settings/session-timeout", methods=["POST"])
@login_required
def update_session_timeout():
    try:
        minutes = settings_service.update_session_timeout(
            request.form.get("session_timeout_minutes")
        )
    except SettingsValidationError as exc:
        flash(str(exc), "danger")
        return _back("security-section")

    activity_log_service.log_action(
        user_id=session.get("user_id"),
        admin_name=session.get("full_name", "Unknown"),
        action=activity_log_service.ACTION_UPDATED_SESSION_SETTINGS,
        details=f"Session timeout set to {minutes} minutes.",
    )

    flash(f"Session timeout set to {minutes} minutes.", "success")
    return _back("security-section")


@settings_bp.route("/settings/activity-log")
@login_required
def activity_log():
    logs = activity_log_service.get_recent_logs(limit=200)
    return render_template("settings/activity_log.html", activity_logs=logs, active_page="settings")