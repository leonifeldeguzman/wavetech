"""add observation rules

Revision ID: 19905b856c20
Revises: c9a3f5e7b2d4
Create Date: 2026-11-06 20:38:32.826515

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '19905b856c20'
down_revision = 'c9a3f5e7b2d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('observation_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=40), nullable=False),
        sa.Column('label', sa.String(length=40), nullable=False),
        sa.Column('effect', sa.String(length=10), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key')
    )


def downgrade():
    op.drop_table('observation_rules')
    # ### end Alembic commands ###
