from flask import Blueprint

# Passenger Portal API blueprint (JSON-only, no templates).
# Mirrors the existing blueprint convention used by auth/dashboard/manifests.
passenger_bp = Blueprint("passenger", __name__)

from app.blueprints.passenger import routes  # noqa: E402, F401