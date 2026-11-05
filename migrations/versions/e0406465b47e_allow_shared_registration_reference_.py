"""allow shared registration reference codes

Revision ID: e0406465b47e
Revises: 0b98ef09edd2
Create Date: 2026-11-04 21:20:53.519727

"""
from alembic import op
import sqlalchemy as sa


revision = "shared_reference_codes"
down_revision = "0b98ef09edd2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    constraints = inspector.get_unique_constraints(
        "pending_registrations"
    )

    unique_name = None

    for constraint in constraints:
        if constraint.get("column_names") == ["reference_code"]:
            unique_name = constraint["name"]
            break

    if unique_name:
        with op.batch_alter_table(
            "pending_registrations"
        ) as batch_op:
            batch_op.drop_constraint(
                unique_name,
                type_="unique"
            )


def downgrade():
    with op.batch_alter_table(
        "pending_registrations"
    ) as batch_op:
        batch_op.create_unique_constraint(
            "uq_pending_registration_reference_code",
            ["reference_code"]
        )
