from flask import render_template, request, redirect, url_for, session, flash
from app.blueprints.auth import auth_bp
from app.extensions import db
from app.models.user import User
from app.services import activity_log_service
from app.utils.security import verify_password
import time
from flask import render_template, request, redirect, url_for, session, flash, current_app
from app.services import password_reset_service
from app.utils.security import verify_password, hash_password   # confirm hash_password exists

def _lockout_message(minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return (
        "Too many failed sign-in attempts. "
        f"Please try again in {minutes} {unit}."
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        admin_id = request.form.get("admin_id")
        password = request.form.get("password")

        # Locked out? Stop before checking the password, so even the correct
        # password is refused until the lockout ends.
        minutes_left = activity_log_service.get_login_lockout_minutes(admin_id)
        if minutes_left:
            flash(_lockout_message(minutes_left))
            return render_template("auth/login.html")

        user = User.query.filter_by(admin_id=admin_id).first()

        if user and verify_password(password, user.password_hash):
            session["user_id"] = user.id
            session["full_name"] = user.full_name
            session["role"] = user.role
            activity_log_service.log_action(
                user_id=user.id,
                admin_name=user.full_name,
                action=activity_log_service.ACTION_ADMIN_LOGIN,
            )
            return redirect(url_for("dashboard.index"))
        else:
            # Record the failed attempt. Only the typed Admin ID is stored,
            # never the password. The on-screen message stays the same for
            # both cases so it doesn't reveal which Admin IDs exist; the
            # reason is kept in the (admin-only) Activity Log.
            try:
                activity_log_service.log_failed_login(
                    username=admin_id,
                    user_id=user.id if user else None,
                    reason=(
                        "Incorrect password."
                        if user
                        else "Admin ID not found."
                    ),
                )
                locked_minutes = activity_log_service.lock_login_if_needed(
                    username=admin_id,
                    user_id=user.id if user else None,
                )
            except Exception:
                # A logging problem must never block or break the login page.
                db.session.rollback()
                locked_minutes = 0

            if locked_minutes:
                flash(_lockout_message(locked_minutes))
            else:
                flash("Invalid Admin ID or password.")

    return render_template("auth/login.html")

@auth_bp.route("/logout")
def logout():
    if "user_id" in session:
        activity_log_service.log_action(
            user_id=session.get("user_id"),
            admin_name=session.get("full_name", "Unknown"),
            action=activity_log_service.ACTION_ADMIN_LOGOUT,
        )
    session.clear()
    return redirect(url_for("auth.login"))

RESET_VERIFIED_WINDOW = 10 * 60  # seconds to finish after the code is verified


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        admin_id = (request.form.get("admin_id") or "").strip()
        user = User.query.filter_by(admin_id=admin_id).first()

        if user and user.email:
            try:
                password_reset_service.send_reset_code(user)
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Could not send reset email")

        # Same answer whether or not the account exists.
        session["reset_admin_id"] = admin_id
        session.pop("reset_verified_user_id", None)
        flash("A code was sent to its registered email.", "info")
        return redirect(url_for("auth.verify_otp"))

    return render_template("auth/forgot_password.html")


@auth_bp.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    admin_id = session.get("reset_admin_id")
    if not admin_id:
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(admin_id=admin_id).first()

    if request.method == "POST":
        code = (request.form.get("otp") or "").strip()
        status = password_reset_service.verify_code(user, code) if user else "invalid"

        if status == "ok":
            session["reset_verified_user_id"] = user.id
            session["reset_verified_at"] = time.time()
            return redirect(url_for("auth.reset_password"))

        if status == "invalid":
            flash("Incorrect code. Please try again.", "danger")
        elif status == "locked":
            flash("Too many wrong attempts. Please request a new code.", "danger")
            return redirect(url_for("auth.forgot_password"))
        else:
            flash("This code has expired. Please request a new one.", "danger")
            return redirect(url_for("auth.forgot_password"))

    masked = (
        password_reset_service.mask_email(user.email)
        if user and user.email
        else None
    )
    return render_template("auth/verify_otp.html", masked_email=masked)


@auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    admin_id = session.get("reset_admin_id")
    if not admin_id:
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(admin_id=admin_id).first()
    if user and user.email:
        try:
            status = password_reset_service.send_reset_code(user)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Could not resend reset email")
            status = "sent"
        if status == "cooldown":
            flash("Please wait a minute before requesting another code.", "danger")
            return redirect(url_for("auth.verify_otp"))
        if status == "limited":
            flash("Too many requests. Please try again later.", "danger")
            return redirect(url_for("auth.verify_otp"))

    flash("A new code was sent.", "info")
    return redirect(url_for("auth.verify_otp"))


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    user_id = session.get("reset_verified_user_id")
    verified_at = session.get("reset_verified_at", 0)
    if not user_id or time.time() - verified_at > RESET_VERIFIED_WINDOW:
        session.pop("reset_verified_user_id", None)
        flash("Please verify your code first.", "danger")
        return redirect(url_for("auth.forgot_password"))

    user = db.session.get(User, user_id)

    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif new_password != confirm:
            flash("Passwords do not match.", "danger")
        else:
            user.password_hash = hash_password(new_password)
            db.session.commit()

            activity_log_service.log_action(
                user_id=user.id,
                admin_name=user.full_name,
                action=getattr(
                    activity_log_service,
                    "ACTION_PASSWORD_RESET",
                    "Password reset",
                ),
            )

            for key in ("reset_admin_id", "reset_verified_user_id", "reset_verified_at"):
                session.pop(key, None)
            flash("Password updated. You can now log in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html")