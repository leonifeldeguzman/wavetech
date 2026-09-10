from app.extensions import db


class SystemSetting(db.Model):
    """Singleton table (always id=1) holding Admin Settings values.

    This is the ONE centralized place Safety Thresholds, the Refresh
    Interval, and the Session Timeout live once an Admin has saved them
    from the Admin Settings page. It is intentionally the single source
    of truth so that Scheduling Decision-Support (and the environmental
    auto-refresh) always evaluate against whatever was last saved here,
    instead of values being duplicated across multiple files.

    Until an Admin saves settings for the first time, `app.config`
    (populated from environment variables — see app/config.py) is used
    as the default/fallback. See app/services/settings_service.py.
    """

    __tablename__ = "system_settings"

    id = db.Column(db.Integer, primary_key=True)

    # --- Safety Thresholds (Scheduling Decision-Support) ---
    # Temporary/system-defined thresholds — NOT official maritime safety
    # limits. See Admin Settings > Safety Thresholds.
    wind_safe_max_kmh = db.Column(db.Float, nullable=False)
    wind_caution_max_kmh = db.Column(db.Float, nullable=False)

    water_safe_min_m = db.Column(db.Float, nullable=False)
    water_safe_max_m = db.Column(db.Float, nullable=False)
    water_caution_min_m = db.Column(db.Float, nullable=False)
    water_caution_max_m = db.Column(db.Float, nullable=False)

    # Comma-separated condition labels, e.g. "Sunny, Partly Cloudy"
    weather_safe = db.Column(db.String(200), nullable=False)
    weather_caution = db.Column(db.String(200), nullable=False)
    weather_unsafe = db.Column(db.String(200), nullable=False)

    # --- Refresh Interval (Lake Condition Monitoring auto-refresh) ---
    refresh_interval_seconds = db.Column(db.Integer, nullable=False)

    # --- Security & Activity ---
    session_timeout_minutes = db.Column(db.Integer, nullable=False)

    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    updated_by = db.relationship("User")

    def __repr__(self):
        return f"<SystemSetting id={self.id}>"
