from flask import Blueprint

monitoring_bp = Blueprint(
    "monitoring",
    __name__,
    template_folder="../../templates/monitoring"
)

from app.blueprints.monitoring import routes  # noqa: E402, F401