from app.extensions import db

from datetime import datetime, timedelta, timezone

from app.models.environmental_reading import EnvironmentalReading
from app.utils.ph_clock import ph_today


class HistoricalWaterLevel(db.Model):
    """LLDA daily lake-wide water level, 2023-2025.

    Reference data only. It is NOT a current reading and is deliberately
    kept out of environmental_readings so "latest reading" queries can
    never pick it up.
    """

    __tablename__ = "historical_water_levels"

    id = db.Column(db.Integer, primary_key=True)

    # One value per calendar day. The unique constraint is what makes
    # the import safe to re-run (we can skip dates that already exist).
    date = db.Column(db.Date, nullable=False, unique=True, index=True)

    level_m = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(10), nullable=False, default="m")
    source = db.Column(db.String(50), nullable=False, default="LLDA historical")

    imported_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<HistoricalWaterLevel {self.date} {self.level_m}{self.unit}>"


