"""add approved_at to registrations, registration_id to manifest_entries

Additive only, both columns nullable. Existing registrations keep
approved_at = NULL (no approval time is invented for rows approved before
this change) and existing manifest entries keep registration_id = NULL; the
manifest shows "Not recorded" for those. registrations.created_at is
untouched.

Revision ID: b6e1d4a8c3f7
Revises: d3f9a1b7c5e2
Create Date: 2026-11-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6e1d4a8c3f7'
down_revision = 'd3f9a1b7c5e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('registrations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approved_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('manifest_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('registration_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_manifest_entries_registration_id'), ['registration_id'], unique=False)
        batch_op.create_foreign_key('fk_manifest_entries_registration_id', 'registrations', ['registration_id'], ['id'])


def downgrade():
    with op.batch_alter_table('manifest_entries', schema=None) as batch_op:
        batch_op.drop_constraint('fk_manifest_entries_registration_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_manifest_entries_registration_id'))
        batch_op.drop_column('registration_id')

    with op.batch_alter_table('registrations', schema=None) as batch_op:
        batch_op.drop_column('approved_at')