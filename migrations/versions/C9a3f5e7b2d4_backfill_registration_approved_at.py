"""backfill approved_at / manifest link for registrations approved earlier

Data-only migration (no schema change). Before b6e1d4a8c3f7 an approval left
no timestamp and the manifest entry had no link to its registration.

For each online manifest entry that has no registration_id yet, find the
single approved passenger on the same trip with the same name (case and
spacing ignored), age and contact number, and link them. Then, for each
registration that is still approved_at = NULL, set approved_at to the
earliest check_in_time of its linked entries: online entries are created at
the moment of approval, so that time is the approval time.

Safe by design:
  * only fills NULLs, never overwrites an existing approved_at/registration_id
  * ambiguous matches (several entries or passengers sharing the same
    identity on a trip) are skipped and stay "Not recorded"
  * registrations.created_at is never touched

Revision ID: c9a3f5e7b2d4
Revises: b6e1d4a8c3f7
Create Date: 2026-11-06 00:00:00.000000

"""
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9a3f5e7b2d4'
down_revision = 'b6e1d4a8c3f7'
branch_labels = None
depends_on = None


manifest_entries = sa.table(
    'manifest_entries',
    sa.column('id', sa.Integer),
    sa.column('trip_id', sa.Integer),
    sa.column('full_name', sa.String),
    sa.column('age', sa.Integer),
    sa.column('contact_number', sa.String),
    sa.column('source', sa.String),
    sa.column('check_in_time', sa.DateTime),
    sa.column('registration_id', sa.Integer),
)
pending_registrations = sa.table(
    'pending_registrations',
    sa.column('id', sa.Integer),
    sa.column('trip_id', sa.Integer),
    sa.column('full_name', sa.String),
    sa.column('age', sa.Integer),
    sa.column('contact_number', sa.String),
    sa.column('status', sa.String),
    sa.column('registration_id', sa.Integer),
)
registrations = sa.table(
    'registrations',
    sa.column('id', sa.Integer),
    sa.column('approved_at', sa.DateTime),
)


def _key(trip_id, full_name, age, contact):
    name = " ".join((full_name or "").split()).lower()
    return (trip_id, name, age, (contact or "").strip())


def upgrade():
    bind = op.get_bind()

    entries = bind.execute(
        sa.select(
            manifest_entries.c.id, manifest_entries.c.trip_id,
            manifest_entries.c.full_name, manifest_entries.c.age,
            manifest_entries.c.contact_number, manifest_entries.c.check_in_time,
        ).where(
            manifest_entries.c.source == 'online',
            manifest_entries.c.registration_id.is_(None),
        )
    ).fetchall()

    passengers = bind.execute(
        sa.select(
            pending_registrations.c.id, pending_registrations.c.trip_id,
            pending_registrations.c.full_name, pending_registrations.c.age,
            pending_registrations.c.contact_number,
            pending_registrations.c.registration_id,
        ).where(
            pending_registrations.c.status == 'approved',
            pending_registrations.c.registration_id.isnot(None),
        )
    ).fetchall()

    entries_by_key = defaultdict(list)
    for e in entries:
        entries_by_key[_key(e.trip_id, e.full_name, e.age, e.contact_number)].append(e)

    passengers_by_key = defaultdict(list)
    for p in passengers:
        passengers_by_key[_key(p.trip_id, p.full_name, p.age, p.contact_number)].append(p)

    earliest = {}   # registration_id -> earliest check_in_time
    for key, es in entries_by_key.items():
        ps = passengers_by_key.get(key, [])
        if len(es) != 1 or len(ps) != 1:
            continue    # no match, or ambiguous: leave as "Not recorded"
        entry, passenger = es[0], ps[0]
        bind.execute(
            manifest_entries.update()
            .where(
                manifest_entries.c.id == entry.id,
                manifest_entries.c.registration_id.is_(None),
            )
            .values(registration_id=passenger.registration_id)
        )
        if entry.check_in_time is not None:
            current = earliest.get(passenger.registration_id)
            if current is None or entry.check_in_time < current:
                earliest[passenger.registration_id] = entry.check_in_time

    for registration_id, moment in earliest.items():
        bind.execute(
            registrations.update()
            .where(
                registrations.c.id == registration_id,
                registrations.c.approved_at.is_(None),
            )
            .values(approved_at=moment)
        )


def downgrade():
    # Data-only: the backfilled values are indistinguishable from real ones,
    # so nothing is reverted here. Downgrading b6e1d4a8c3f7 drops the columns.
    pass