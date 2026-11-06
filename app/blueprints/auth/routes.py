from flask import render_template, request, redirect, url_for, session, flash
from app.blueprints.auth import auth_bp
from app.extensions import db
from app.models.user import User
from app.services import activity_log_service
from app.utils.security import verify_password

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