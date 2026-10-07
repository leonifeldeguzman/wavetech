"""LLDA water quality monitoring stations near Talim Island.

Coordinates come from LLDA's station shapefile (LDeGuzman_LdB_POI_WQMS),
converted from UTM zone 51N to WGS84 latitude/longitude.
These are LOCATIONS only. Water level is lake-wide and is never stored
per station.
"""
from app.extensions import db


class LakeStation(db.Model):
    __tablename__ = "lake_stations"

    id = db.Column(db.Integer, primary_key=True)
    station_no = db.Column(db.String(10), nullable=False, unique=True)  # "I", "IV", "XX"
    name = db.Column(db.String(100), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    coords_source = db.Column(db.String(100), nullable=False,
                              default="LLDA station shapefile")
    coords_verified = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<LakeStation {self.station_no} {self.name}>"