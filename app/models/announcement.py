from app.extensions import db
from app.utils.timezone import utc_now_naive


class Announcement(db.Model):
    """Passenger-facing notices (Safety Alert, Delay, Cancellation, General
    Notice).

    User-facing terminology is "Notice"; the class and table keep their
    original "Announcement"/"announcements" names so no table-rename
    migration is needed.

    Lifecycle (see app/services/announcement_service.py, the only module
    that should create/update rows here):

      - "Publish Now": status="Active", published_at=now, scheduled_at=None.
      - "Schedule": status="Inactive", scheduled_at=<future time>,
        published_at=None. It stays this way until
        announcement_service.publish_due_announcements() flips it to
        status="Active" (and stamps published_at) once scheduled_at has
        passed.
      - "Save as Draft": status="Inactive", published_at=None,
        scheduled_at=None.
      - Unpublishing a published notice sets status="Inactive" without
        touching published_at, so the row keeps its history. Cancelling a
        still-scheduled notice clears scheduled_at (it becomes a Draft).
      - Re-publishing sets status="Active" again.

    User-facing status (see `display_status`):

      Draft       Inactive, never published, no schedule.
      Scheduled   Inactive, never published, scheduled_at set.
      Published   Active and not past `expires_at`.
      Unpublished Inactive but previously published (manual action).
      Expired     Active but `expires_at` has passed.

    "Expired" is derived from `expires_at` at read time and enforced in the
    backend queries (see announcement_service.get_published_announcements),
    so a notice stops being public the instant its expiry passes, whether
    or not any background job has run. Expired rows are never deleted.
    """

    __tablename__ = "announcements"

    # Notice types (stored in `type`).
    TYPE_SAFETY_ALERT = "Safety Alert"
    TYPE_DELAY = "Delay"
    TYPE_CANCELLATION = "Cancellation"
    TYPE_GENERAL = "General Notice"
    # Backwards-compatible alias: older code/tests call the default type
    # TYPE_ANNOUNCEMENT. It now maps to the "General Notice" type.
    TYPE_ANNOUNCEMENT = TYPE_GENERAL
    # Value stored by rows created before the QA change. Migrated to
    # "General Notice", and still accepted (and normalized) on input.
    LEGACY_TYPE_ANNOUNCEMENT = "Announcement"

    ALL_TYPES = (
        TYPE_SAFETY_ALERT,
        TYPE_DELAY,
        TYPE_CANCELLATION,
        TYPE_GENERAL,
    )

    # Stored status values (unchanged).
    STATUS_ACTIVE = "Active"
    STATUS_INACTIVE = "Inactive"

    # User-facing status labels (derived, see display_status).
    LABEL_DRAFT = "Draft"
    LABEL_SCHEDULED = "Scheduled"
    LABEL_PUBLISHED = "Published"
    LABEL_UNPUBLISHED = "Unpublished"
    LABEL_EXPIRED = "Expired"

    _TYPE_SLUGS = {
        TYPE_SAFETY_ALERT: "safety",
        TYPE_DELAY: "delay",
        TYPE_CANCELLATION: "cancellation",
        TYPE_GENERAL: "general",
    }

    _TYPE_ICONS = {
        TYPE_SAFETY_ALERT: "bi-exclamation-triangle-fill",
        TYPE_DELAY: "bi-clock-history",
        TYPE_CANCELLATION: "bi-x-octagon-fill",
        TYPE_GENERAL: "bi-info-circle",
    }

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)

    type = db.Column(db.String(20), nullable=False, default=TYPE_GENERAL)
    # "Safety Alert", "Delay", "Cancellation" or "General Notice"

    status = db.Column(db.String(20), nullable=False, default=STATUS_INACTIVE)
    # "Active" or "Inactive" (see class docstring for user-facing labels)

    scheduled_at = db.Column(db.DateTime, nullable=True)
    # When set + status is Inactive + published_at is still None, this row
    # is "Scheduled" (pending future publish). Naive UTC datetime.

    published_at = db.Column(db.DateTime, nullable=True)
    # Stamped the moment the notice actually goes live, whether via
    # "Publish Now" or the scheduled time being reached.

    expires_at = db.Column(db.DateTime, nullable=True)
    # Optional. Naive UTC datetime. When set and now >= expires_at, the
    # notice is Expired and is never shown to passengers. When NULL the
    # notice has no expiry: it stays published until manually unpublished.

    sms_dispatched_at = db.Column(db.DateTime, nullable=True)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    trip_id = db.Column(db.Integer, db.ForeignKey("trips.id"), nullable=True)
    trip = db.relationship("Trip")

    created_by = db.relationship("User")

    # ------------------------------------------------------------------
    # Derived helpers used by the templates
    # ------------------------------------------------------------------

    @property
    def type_slug(self) -> str:
        """CSS modifier: safety / delay / cancellation / general."""
        return self._TYPE_SLUGS.get(self.type, "general")

    @property
    def type_icon(self) -> str:
        """Bootstrap Icons class for this notice type."""
        return self._TYPE_ICONS.get(self.type, self._TYPE_ICONS[self.TYPE_GENERAL])

    @property
    def is_expired(self) -> bool:
        """True once `expires_at` has passed for a notice that went live."""
        return (
            self.status == self.STATUS_ACTIVE
            and self.expires_at is not None
            and self.expires_at <= utc_now_naive()
        )

    @property
    def display_status(self) -> str:
        if self.status == self.STATUS_ACTIVE:
            return self.LABEL_EXPIRED if self.is_expired else self.LABEL_PUBLISHED
        if self.published_at is not None:
            return self.LABEL_UNPUBLISHED
        if self.scheduled_at is not None:
            return self.LABEL_SCHEDULED
        return self.LABEL_DRAFT

    def __repr__(self):
        return f"<Announcement {self.type} '{self.title}' ({self.status})>"
