from flask import render_template, request, redirect, url_for, session, flash
from app.blueprints.auth import auth_bp
from app.models.user import User
from app.utils.security import verify_password

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        admin_id = request.form.get("admin_id")
        password = request.form.get("password")

        user = User.query.filter_by(admin_id=admin_id).first()

        if user and verify_password(password, user.password_hash):
            session["user_id"] = user.id
            session["full_name"] = user.full_name
            session["role"] = user.role
            return redirect(url_for("dashboard.index"))
        else:
            flash("Invalid Admin ID or password.")

    return render_template("auth/login.html")

@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))