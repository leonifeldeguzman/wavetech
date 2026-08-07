from app.extensions import db

class ManifestEntry(db.Model):
    __tablename__ = "manifest_entries"

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.Integer, db.ForeignKey("trips.id"), nullable=False)

    full_name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    address = db.Column(db.String(255), nullable=True)
    contact_number = db.Column(db.String(20), nullable=True)
    passenger_type = db.Column(db.String(20), nullable=False, default="Regular")
    # Regular, Student, Senior, PWD, Child

    check_in_time = db.Column(db.DateTime, server_default=db.func.now())
    source = db.Column(db.String(10), nullable=False, default="walkin")
    # walkin or online

    trip = db.relationship("Trip", backref="manifest_entries")

    def __repr__(self):
        return f"<ManifestEntry {self.full_name}>"