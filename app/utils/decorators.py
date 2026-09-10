from functools import wraps
from flask import session, redirect, url_for, abort

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Like login_required, but also requires session["role"] == "Admin".

    Used to protect Admin-only Settings (Safety Thresholds, Security &
    Activity) so Operators cannot view or modify them.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        if session.get("role") != "Admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function