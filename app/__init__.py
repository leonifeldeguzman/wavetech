import os
from datetime import timedelta

from flask import Flask, session
from app.config import Config
from app.extensions import db, migrate


def _should_start_announcement_scheduler(app) -> bool:
    """Decide whether this process should run the Announcement background
    scheduler (see app/services/announcement_service.py).

    - Never in tests (TESTING=True): tests exercise publish_due_
      announcements() directly, or via the route-level fallback, so they
      don't need a real background thread ticking during the run.
    - Under Flask's debug reloader, the reloader's parent "watcher"
      process re-imports and runs this whole module too, but only the
      actual serving child process has WERKZEUG_RUN_MAIN=true — starting
      the scheduler in both would be a second, conflicting instance.
    """
    if app.config.get("TESTING"):
        return False
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)

    from app.utils.timezone import format_ph_time
    app.jinja_env.filters["ph_time"] = format_ph_time

    from app import models

    from app.blueprints.auth import auth_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.manifests import manifests_bp
    from app.blueprints.passenger import passenger_bp
    from app.blueprints.monitoring import monitoring_bp
    from app.blueprints.settings import settings_bp
    from app.blueprints.announcements import announcements_bp
    app.register_blueprint(monitoring_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(manifests_bp)
    app.register_blueprint(passenger_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(announcements_bp)

    from app.models.user import User
    from app.models.boat import Boat
    from app.models.trip import Trip
    from app.models.manifest_entry import ManifestEntry
    from app.models.pending_registration import PendingRegistration
    from app.models.environmental_reading import EnvironmentalReading
    from app.models.system_setting import SystemSetting
    from app.models.activity_log import ActivityLog

    @app.before_request
    def _apply_session_timeout():
        # Session Settings (Admin Settings > Security & Activity): the
        # configured timeout controls how long a logged-in session stays
        # valid, reusing Flask's existing session/cookie mechanism rather
        # than introducing a second auth system.
        if "user_id" in session:
            from app.services import settings_service

            try:
                timeout_minutes = settings_service.get_session_timeout_minutes()
            except Exception:
                timeout_minutes = 30
            session.permanent = True
            app.permanent_session_lifetime = timedelta(minutes=timeout_minutes)

    @app.route("/")
    def index():
        return "WaveTech is running!"

    # Announcement Management: automatically publish Scheduled
    # announcements whose time has arrived, without requiring anyone to
    # open or refresh a page (see app/services/announcement_service.py).
    if _should_start_announcement_scheduler(app):
        from app.services import announcement_service

        announcement_service.start_background_scheduler(app)

    return app