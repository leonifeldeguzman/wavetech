"""
Test fixtures for the Passenger Portal API.

By default this uses a disposable, in-memory-speed SQLite file for fast
local test runs. That's fine for everything except the concurrency test
(test_concurrent_registration_last_seat), because SQLite treats
SELECT...FOR UPDATE as a documented no-op — it never actually locks the
row — so it can't prove the row-lock guarantee that only matters on
Postgres.

To run against a real Postgres database instead, set TEST_DATABASE_URL
(NOT DATABASE_URL — see the safety note below) to a DEDICATED, EMPTY
test database before invoking pytest, e.g. in PowerShell:

    $env:TEST_DATABASE_URL = "postgresql://postgres:<password>@localhost:5432/wavetech_test"
    python -m pytest app/tests/test_passenger_portal.py -v

IMPORTANT SAFETY NOTE:
Every test in this file tears down with db.drop_all() against whatever
database it's pointed at. This fixture deliberately reads a SEPARATE
env var (TEST_DATABASE_URL) rather than reusing your app's normal
DATABASE_URL from .env, specifically so that forgetting to set anything
falls back to a harmless disposable SQLite file instead of silently
running against — and dropping tables in — your real development
database. Do not set TEST_DATABASE_URL to the same value as the
DATABASE_URL in your .env file. Use a differently-named database
(e.g. wavetech_test vs. wavetech_dev) that you create specifically for
this purpose and don't mind being wiped repeatedly.
"""
import os
import tempfile
import warnings

import pytest

_test_db_url = os.environ.get("TEST_DATABASE_URL")

if _test_db_url:
    if "test" not in _test_db_url.lower():
        warnings.warn(
            "TEST_DATABASE_URL does not contain 'test' in its name. "
            "These tests call db.drop_all() at the end of every test "
            "function. Double-check this is a dedicated, disposable "
            "test database and NOT your development database before "
            "proceeding.",
            stacklevel=1,
        )
    os.environ["DATABASE_URL"] = _test_db_url
else:
    _db_fd, _db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.boat import Boat  # noqa: E402
from app.models.trip import Trip  # noqa: E402
from app.models.pending_registration import PendingRegistration  # noqa: E402
from app.models.manifest_entry import ManifestEntry  # noqa: E402


@pytest.fixture()
def app():
    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app):
    """A test client that's already "logged in" as an admin/operator.

    login_required (app/utils/decorators.py) only checks for
    session["user_id"] — it does not look up a User row — so this
    fixture doesn't need to create one. It's still a faithful test of
    the real decorator behavior, just skipping the login form itself.
    """
    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["full_name"] = "Test Admin"
        sess["role"] = "Operator"
    return test_client


@pytest.fixture()
def make_boat(app):
    def _make(name="Test Boat", capacity=3):
        with app.app_context():
            boat = Boat(name=name, capacity=capacity)
            db.session.add(boat)
            db.session.commit()
            return boat.id
    return _make


@pytest.fixture()
def make_trip(app):
    def _make(boat_id=None, status="Open", **kwargs):
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            trip = Trip(
                boat_id=boat_id,
                departure_time=kwargs.pop("departure_time", datetime.now(timezone.utc) + timedelta(hours=2)),
                status=status,
                **kwargs,
            )
            db.session.add(trip)
            db.session.commit()
            return trip.id
    return _make