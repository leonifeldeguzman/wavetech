from app.extensions import db
from app.utils.timezone import utc_now_naive


class UserProfilePicture(db.Model):
    """Optional custom profile picture for a user (one row per user).

    Kept in its own table, stored in the database itself, so that:
      * it persists across page refreshes, re-login and redeploys (no
        dependence on a local uploads/ folder that hosting may wipe), and
      * the image bytes are never loaded by ordinary `User` queries.

    No row = the user has no custom picture, and the UI shows the default
    avatar. See app/services/profile_picture_service.py.
    """

    __tablename__ = "user_profile_pictures"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    image_data = db.Column(db.LargeBinary, nullable=False)
    content_type = db.Column(db.String(50), nullable=False)

    # Naive UTC, per the project convention (see app/utils/timezone.py).
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )

    def __repr__(self):
        return f"<UserProfilePicture user_id={self.user_id}>"