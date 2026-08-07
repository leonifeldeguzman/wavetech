from app import create_app
from app.extensions import db
from app.models.user import User
from app.utils.security import hash_password

app = create_app()

with app.app_context():
    existing = User.query.filter_by(admin_id="admin001").first()

    if existing:
        print("Admin account already exists — skipping.")
    else:
        new_admin = User(
            admin_id="admin001",
            password_hash=hash_password("changeme123"),
            full_name="Capt. Juan",
            role="Operator"
        )
        db.session.add(new_admin)
        db.session.commit()
        print("Admin account created successfully!")
        print("Admin ID: admin001")
        print("Password: changeme123")