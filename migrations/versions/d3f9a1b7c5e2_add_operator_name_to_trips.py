"""add operator_name to trips

Additive only: one nullable column. Existing trips keep operator_name = NULL
(the operator is not the same person as the crew, so nothing is backfilled).

Revision ID: d3f9a1b7c5e2
Revises: 678ffdcd9b2c
Create Date: 2026-11-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3f9a1b7c5e2'
down_revision = '678ffdcd9b2c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('operator_name', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.drop_column('operator_name')
