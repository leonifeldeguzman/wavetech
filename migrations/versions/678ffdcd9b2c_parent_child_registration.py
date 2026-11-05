"""parent_child_registration

Revision ID: 678ffdcd9b2c
Revises: shared_reference_codes
Create Date: 2026-11-05 09:09:25.534326

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '678ffdcd9b2c'
down_revision = 'shared_reference_codes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registrations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("reference_code", sa.String(20), nullable=False),
        sa.Column("trip_id", sa.Integer, sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("registration_type", sa.String(10), nullable=False,
                  server_default="solo"),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("created_at", sa.DateTime),
        sa.Column("cancelled_at", sa.DateTime),
        sa.CheckConstraint("registration_type IN ('solo','group')",
                           name="ck_registrations_type"),
    )
    op.create_index("ix_registrations_reference_code", "registrations",
                    ["reference_code"], unique=True)
    op.create_index("ix_registrations_trip_id", "registrations", ["trip_id"])

    op.create_table(
        "registration_cancellations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("registration_id", sa.Integer,
                  sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("cancel_type", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(200)),
        sa.Column("cancelled_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_registration_cancellations_registration_id",
                    "registration_cancellations", ["registration_id"])

    op.create_table(
        "verification_throttles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope_key", sa.String(100), nullable=False, unique=True),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime, nullable=False),
        sa.Column("locked_until", sa.DateTime),
    )

    op.create_table(
        "cancellation_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("registration_id", sa.Integer,
                  sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("consumed_at", sa.DateTime),
    )
    op.create_index("ix_cancellation_tokens_registration_id",
                    "cancellation_tokens", ["registration_id"])

    with op.batch_alter_table("pending_registrations") as b:
        b.add_column(sa.Column("registration_id", sa.Integer))
        b.add_column(sa.Column("cancelled_at", sa.DateTime))
        b.add_column(sa.Column("cancellation_id", sa.Integer))
        b.create_foreign_key("fk_pending_registration_parent",
                             "registrations", ["registration_id"], ["id"])
        b.create_foreign_key("fk_pending_registration_cancellation",
                             "registration_cancellations",
                             ["cancellation_id"], ["id"])
        b.create_index("ix_pending_registrations_registration_id",
                       ["registration_id"])

    _backfill()


def _backfill():
    """One parent per existing reference_code; link every passenger row.

    Idempotent: a reference code that already has a parent is reused, and
    only passenger rows with registration_id IS NULL are updated. Passenger
    rows, reference codes and details are never modified apart from the new
    registration_id link.
    """
    conn = op.get_bind()

    # Declare the primary key so SQLAlchemy knows what "id" is. (The id is
    # fetched with RETURNING below, which does not depend on this, but it
    # keeps the table definition honest.)
    pr = sa.table(
        "pending_registrations",
        sa.column("id", sa.Integer),
        sa.column("trip_id", sa.Integer),
        sa.column("reference_code", sa.String),
        sa.column("status", sa.String),
        sa.column("submitted_at", sa.DateTime),
        sa.column("registration_id", sa.Integer),
    )
    reg = sa.table(
        "registrations",
        sa.column("id", sa.Integer),
        sa.column("reference_code", sa.String),
        sa.column("trip_id", sa.Integer),
        sa.column("registration_type", sa.String),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime),
    )

    rows = conn.execute(
        sa.select(pr.c.id, pr.c.trip_id, pr.c.reference_code, pr.c.status,
                  pr.c.submitted_at).order_by(pr.c.id)
    ).fetchall()

    groups = {}
    for r in rows:
        groups.setdefault(r.reference_code, []).append(r)

    clashes = [c for c, g in groups.items() if len({x.trip_id for x in g}) > 1]
    if clashes:
        raise RuntimeError(
            "Reference codes shared across different trips (resolve manually "
            f"before migrating): {clashes[:10]}"
        )

    for code, g in groups.items():
        # Reuse an existing parent if one is already there (safe re-run).
        parent_id = conn.execute(
            sa.select(reg.c.id).where(reg.c.reference_code == code)
        ).scalar()

        if parent_id is None:
            statuses = [x.status for x in g]
            active = [s for s in statuses if s in ("pending", "approved")]
            if active:
                status = "pending" if "pending" in active else "approved"
            elif "cancelled" in statuses:
                status = "cancelled"
            elif statuses and all(s == "rejected" for s in statuses):
                status = "rejected"
            else:
                status = "pending"

            # INSERT ... RETURNING id (PostgreSQL). Do not rely on
            # inserted_primary_key: lightweight sa.table() objects have no
            # declared primary key, so it comes back as an empty tuple.
            parent_id = conn.execute(
                reg.insert()
                .values(
                    reference_code=code,
                    trip_id=g[0].trip_id,
                    registration_type="group" if len(g) > 1 else "solo",
                    status=status,
                    created_at=min(
                        (x.submitted_at for x in g if x.submitted_at),
                        default=None,
                    ),
                )
                .returning(reg.c.id)
            ).scalar_one()

        conn.execute(
            pr.update()
            .where(pr.c.reference_code == code)
            .where(pr.c.registration_id.is_(None))
            .values(registration_id=parent_id)
        )

    # ---- integrity checks: abort (and roll back) if anything is off ------
    unlinked = conn.execute(
        sa.select(sa.func.count()).select_from(pr)
        .where(pr.c.registration_id.is_(None))
    ).scalar()
    if unlinked:
        raise RuntimeError(f"{unlinked} passenger rows were not linked to a parent")

    parents = conn.execute(sa.select(sa.func.count()).select_from(reg)).scalar()
    if parents != len(groups):
        raise RuntimeError(
            f"Expected {len(groups)} parent registrations, found {parents}"
        )
    # Legacy cancellations keep status='cancelled' but have no
    # cancelled_at / cancellation_id (that data was never recorded).


def downgrade():
    with op.batch_alter_table("pending_registrations") as b:
        b.drop_index("ix_pending_registrations_registration_id")
        b.drop_constraint("fk_pending_registration_cancellation", type_="foreignkey")
        b.drop_constraint("fk_pending_registration_parent", type_="foreignkey")
        b.drop_column("cancellation_id")
        b.drop_column("cancelled_at")
        b.drop_column("registration_id")
    op.drop_table("cancellation_tokens")
    op.drop_table("verification_throttles")
    op.drop_table("registration_cancellations")
    op.drop_table("registrations")