"""add otp tables

Revision ID: b3ee5e51eac5
Revises: 884720b86a9f
Create Date: 2026-11-07 21:32:40.459381

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3ee5e51eac5'
down_revision = '884720b86a9f'
branch_labels = None
depends_on = None


def upgrade():
    # IF NOT EXISTS: these tables were first created by hand, so this does
    # nothing where they already exist and creates them everywhere else.
    op.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            otp_hash VARCHAR(128) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_password_reset_otps_user_id
        ON password_reset_otps (user_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_change_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            new_email VARCHAR(120) NOT NULL,
            otp_hash VARCHAR(128) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_change_requests_user_id
        ON email_change_requests (user_id)
    """)
    op.execute("ALTER TABLE password_reset_otps ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_change_requests ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.execute("DROP TABLE IF EXISTS email_change_requests")
    op.execute("DROP TABLE IF EXISTS password_reset_otps")