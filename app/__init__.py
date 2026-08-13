from flask import Flask, app
from app.config import Config
from app.extensions import db, migrate

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)

    from app import models

    from app.blueprints.auth import auth_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.manifests import manifests_bp
    from app.blueprints.passenger import passenger_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(manifests_bp)
    app.register_blueprint(passenger_bp)

    from app.models.user import User
    from app.models.boat import Boat
    from app.models.trip import Trip
    from app.models.manifest_entry import ManifestEntry
    from app.models.pending_registration import PendingRegistration

    @app.route("/")
    def index():
        return "WaveTech is running!"

    return app