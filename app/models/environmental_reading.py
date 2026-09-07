from app.extensions import db

class EnvironmentalReading(db.Model):
    __tablename__ = "environmental_readings"

    id = db.Column(db.Integer, primary_key=True)

    source = db.Column(db.String(20), nullable=False, default="manual")
    # "mock", "llda", "pagasa", or "manual" — tracks where this reading came from

    location_label = db.Column(db.String(100), nullable=False, default="Central Bay, Cardona, Rizal")

    water_level_m = db.Column(db.Float, nullable=True)
    wave_height_m = db.Column(db.Float, nullable=True)
    wind_speed_kmh = db.Column(db.Float, nullable=True)
    wind_direction = db.Column(db.String(10), nullable=True)
    weather_condition = db.Column(db.String(50), nullable=True)
    temperature_c = db.Column(db.Float, nullable=True)

    entered_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Only set when source == "manual" — who encoded it

    recorded_at = db.Column(db.DateTime, nullable=True)
    # The timestamp the SOURCE (e.g. LLDA) reports for the reading itself.
    # Distinct from retrieved_at below, which is when WaveTech pulled it.
    # Null for manual entries, where "recorded" and "retrieved" are the same moment.

    retrieved_at = db.Column(db.DateTime, server_default=db.func.now())
    # When WaveTech stored this row. For "llda" rows this is also the
    # moment the live fetch succeeded, so it doubles as the "Retrieved"
    # timestamp shown on the dashboard when a cached reading is displayed.

    entered_by = db.relationship("User")

    def __repr__(self):
        return f"<EnvironmentalReading {self.source} @ {self.retrieved_at}>"