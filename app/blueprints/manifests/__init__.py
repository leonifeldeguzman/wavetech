from flask import Blueprint

manifests_bp = Blueprint("manifests", __name__, template_folder="../../templates/manifests")

from app.blueprints.manifests import routes  # noqa: E402, F401