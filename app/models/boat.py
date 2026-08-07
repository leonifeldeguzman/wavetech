from app.extensions import db

class Boat(db.Model):
    __tablename__ = "boats"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)

    def __repr__(self):
        return f"<Boat {self.name}>"