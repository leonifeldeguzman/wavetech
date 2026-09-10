from app.extensions import db


class ActivityLog(db.Model):
    """Admin-only audit trail of important administrative actions.

    Never store passwords, API keys, tokens, or other credentials in
    `details` — see app/services/activity_log_service.py, which is the
    only place rows should be created from.
    """

    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)

    # Nullable + a snapshotted name so the log entry stays readable even
    # if the underlying user account is later removed.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    admin_name = db.Column(db.String(100), nullable=False)

    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship("User")

    def __repr__(self):
        return f"<ActivityLog {self.action} by {self.admin_name}>"
