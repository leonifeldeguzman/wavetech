from app.extensions import db


class Announcement(db.Model):
    """Passenger-facing announcements and safety alerts.

    Lifecycle (see app/services/announcement_service.py, the only module
    that should create/update rows here):

      - "Publish Now": status="Active", published_at=now, scheduled_at=None.
      - "Schedule": status="Inactive", scheduled_at=<future time>,
        published_at=None. It stays this way until
        announcement_service.publish_due_announcements() flips it to
        status="Active" (and stamps published_at) once scheduled_at has
        passed. That helper is called at the top of the admin management
        page and the passenger-facing announcements page, so no separate
        scheduler/cron process is required (see task requirement 9).
      - Deactivating a published announcement (or cancelling a still-
        scheduled one) sets status="Inactive" without touching
        published_at/scheduled_at, so the row keeps its history.
      - Reactivating sets status="Active" again (and stamps published_at
        if it was somehow never set).
    """

    __tablename__ = "announcements"

    TYPE_ANNOUNCEMENT = "Announcement"
    TYPE_SAFETY_ALERT = "Safety Alert"

    STATUS_ACTIVE = "Active"
    STATUS_INACTIVE = "Inactive"

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)

    type = db.Column(db.String(20), nullable=False, default=TYPE_ANNOUNCEMENT)
    # "Announcement" or "Safety Alert"

    status = db.Column(db.String(20), nullable=False, default=STATUS_INACTIVE)
    # "Active" or "Inactive"

    scheduled_at = db.Column(db.DateTime, nullable=True)
    # When set + status is Inactive + published_at is still None, this row
    # is "Scheduled" (pending future publish). Naive datetime, compared
    # against datetime.now() the same way Trip.departure_time already is
    # elsewhere in this codebase (see dashboard/routes.py).

    published_at = db.Column(db.DateTime, nullable=True)
    # Stamped the moment the announcement actually goes live, whether via
    # "Publish Now" or the scheduled time being reached.

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    created_by = db.relationship("User")

    def __repr__(self):
        return f"<Announcement {self.type} '{self.title}' ({self.status})>"