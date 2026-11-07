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

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from app.services import email_change_service
from app.services.password_reset_service import mask_email

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

        # --- Email (controls password reset, so changing it needs the password) ---
    new_email = request.form.get("email", "").strip().lower() or None
    old_email = (user.email or "").lower() or None
    email_changed = new_email != old_email

    if email_changed:
        current_password = request.form.get("current_password", "")
        if not verify_password(current_password, user.password_hash):
            flash("Enter your current password to change your email.", "danger")
            return _back("account-section")

        if new_email:
            if len(new_email) > 120 or not _EMAIL_RE.match(new_email):
                flash("Please enter a valid email address.", "danger")
                return _back("account-section")

            taken = User.query.filter(
                User.email == new_email, User.id != user.id
            ).first()
            if taken:
                flash("That email is already used by another admin.", "danger")
                return _back("account-section")

    full_name = f"{first_name} {last_name}"

    user.full_name = full_name
    session["full_name"] = full_name
    user.phone = phone

    if email_changed:
        user.email = new_email

    db.session.commit()

    if email_changed:
        activity_log_service.log_action(
            user_id=user.id,
            admin_name=user.full_name,
            action=getattr(activity_log_service, "ACTION_UPDATED_EMAIL", "Updated email"),
            details="Recovery email changed." if new_email else "Recovery email removed.",
        )

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


@settings_bp.route("/settings/email-change/request", methods=["POST"])
@login_required
def email_change_request():
    user = db.session.get(User, session["user_id"])
    data = request.get_json(silent=True) or {}

    email, error = email_change_service.clean_email(data.get("email"), user.id)
    if error:
        return jsonify(ok=False, message=error), 400

    if email == (user.email or "").lower():
        return jsonify(ok=False, message="That is already your email."), 400

    # First email ever: there is no old inbox to verify, so ask for the password.
    if not user.email:
        return jsonify(ok=True, mode="password")

    try:
        status = email_change_service.start(user, email)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not send email change code")
        return jsonify(ok=False, message="Could not send the code. Please try again."), 500

    if status == "cooldown":
        return jsonify(ok=False, message="Please wait a minute before requesting another code."), 429
    if status == "limited":
        return jsonify(ok=False, message="Too many requests. Please try again later."), 429

    return jsonify(
        ok=True,
        mode="code",
        sent_to=mask_email(user.email),
        expires_in=int(email_change_service.OTP_TTL.total_seconds()),
    )


@settings_bp.route("/settings/email-change/confirm", methods=["POST"])
@login_required
def email_change_confirm():
    user = db.session.get(User, session["user_id"])
    data = request.get_json(silent=True) or {}

    # First email ever: confirm with the current password.
    if not user.email:
        email, error = email_change_service.clean_email(data.get("email"), user.id)
        if error:
            return jsonify(ok=False, message=error), 400
        if not verify_password(str(data.get("password") or ""), user.password_hash):
            return jsonify(ok=False, message="Current password is incorrect."), 400
        user.email = email
        db.session.commit()
        status = "ok"
    else:
        status, _ = email_change_service.verify(user, str(data.get("code") or "").strip())

    if status == "ok":
        activity_log_service.log_action(
            user_id=user.id,
            admin_name=user.full_name,
            action=getattr(activity_log_service, "ACTION_UPDATED_EMAIL", "Updated email"),
            details="Recovery email changed.",
        )
        flash("Email updated successfully.", "success")
        return jsonify(ok=True)

    if status == "invalid":
        return jsonify(ok=False, message="Incorrect code. Please try again.")
    if status == "taken":
        return jsonify(ok=False, discarded=True, message="That email is already used by another admin.")
    if status == "locked":
        return jsonify(ok=False, discarded=True, message="Too many wrong attempts. The email change was discarded.")
    return jsonify(ok=False, discarded=True, message="The code expired. The email change was discarded.")


@settings_bp.route("/settings/email-change/cancel", methods=["POST"])
@login_required
def email_change_cancel():
    user = db.session.get(User, session["user_id"])
    email_change_service.cancel(user)
    return jsonify(ok=True)