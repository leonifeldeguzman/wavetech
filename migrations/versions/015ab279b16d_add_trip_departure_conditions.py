"""add trip departure_conditions

Revision ID: 015ab279b16d
Revises: 19905b856c20
Create Date: 2026-11-07 15:17:33.240735

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '015ab279b16d'
down_revision = '19905b856c20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('departure_conditions', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.drop_column('departure_conditions')