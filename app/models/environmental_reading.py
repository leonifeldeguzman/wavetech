from app.extensions import db
from datetime import datetime, timezone

class EnvironmentalReading(db.Model):
    __tablename__ = "environmental_readings"

    id = db.Column(db.Integer, primary_key=True)

    source = db.Column(db.String(20), nullable=False, default="manual")
    # "mock", "llda", "pagasa", or "manual" — tracks where this reading came from

    location_label = db.Column(db.String(100), nullable=False, default="Central Bay, Cardona, Rizal")

    water_level_m = db.Column(db.Float, nullable=True)
    wind_speed_kmh = db.Column(db.Float, nullable=True)
    wind_direction = db.Column(db.String(10), nullable=True)
    weather_condition = db.Column(db.String(50), nullable=True)
    temperature_c = db.Column(db.Float, nullable=True)
    observed_condition = db.Column(db.String(20), nullable=True)
    # Operator's structured water-surface observation (calm / moderate / rough).
    # NULL means "not observed". Manual rows only.

    observer_comment = db.Column(db.String(300), nullable=True)
    # Optional free-text note from the operator. Displayed only; never read by rules.

    entered_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Only set when source == "manual" — who encoded it

    OBS_CALM = "calm"
    OBS_MODERATE = "moderate"
    OBS_ROUGH = "rough"
    OBSERVED_CONDITIONS = (OBS_CALM, OBS_MODERATE, OBS_ROUGH)

    station_id = db.Column(
        db.Integer,
        db.ForeignKey("lake_stations.id", name="fk_environmental_readings_station_id"),
        nullable=True,
        index=True,
    )
    # Which LLDA station the WEATHER values belong to. NULL for older rows
    # (the single configured point) and always NULL for manual / LLDA
    # water-level rows: water level is lake-wide, never per station.

    recorded_at = db.Column(db.DateTime, nullable=True)
    # The timestamp the SOURCE (e.g. LLDA) reports for the reading itself.
    # Distinct from retrieved_at below, which is when WaveTech pulled it.
    # Null for manual entries, where "recorded" and "retrieved" are the same moment.

    retrieved_at = db.Column(
    db.DateTime,
    default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    server_default=db.func.now(),
)

    entered_by = db.relationship("User")

    def __repr__(self):
        return f"<EnvironmentalReading {self.source} @ {self.retrieved_at}>"