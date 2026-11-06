from app.extensions import db


class ObservationRule(db.Model):
    """One operator observation (e.g. Calm / Moderate / Rough) and its effect
    on the safety assessment. Managed from Settings -> Safety Thresholds.

    `key` is what water readings store in `observed_condition`. It is created
    from the first label and never changes, so renaming an observation does
    not orphan readings that were already recorded.
    """

    __tablename__ = "observation_rules"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), nullable=False, unique=True)
    label = db.Column(db.String(40), nullable=False)
    # "none" | "caution" | "unsafe"
    effect = db.Column(db.String(10), nullable=False, default="none")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    updated_by_user_id = db.Column(db.Integer, nullable=True)