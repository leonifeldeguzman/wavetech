"""notices: add expires_at, rename legacy type to General Notice

Revision ID: c7d41a9e5b20
Revises: b271dc31fce7
Create Date: 2026-11-03 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d41a9e5b20'
down_revision = 'b271dc31fce7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(), nullable=True))

    # The default type "Announcement" is now the "General Notice" type.
    op.execute(
        "UPDATE announcements SET type = 'General Notice' "
        "WHERE type = 'Announcement'"
    )


def downgrade():
    op.execute(
        "UPDATE announcements SET type = 'Announcement' "
        "WHERE type = 'General Notice'"
    )
    # Delay/Cancellation do not exist in the old schema; fold them back
    # into the closest old value so the old code keeps working.
    op.execute(
        "UPDATE announcements SET type = 'Announcement' "
        "WHERE type IN ('Delay', 'Cancellation')"
    )

    with op.batch_alter_table('announcements', schema=None) as batch_op:
        batch_op.drop_column('expires_at')
