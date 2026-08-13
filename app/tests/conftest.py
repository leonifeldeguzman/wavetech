"""
Test fixtures for the Passenger Portal API.

Uses a temporary SQLite file instead of Postgres purely for local test
convenience/speed. NOTE: SQLite does not enforce real row-level locking
the same way Postgres does, so the concurrency test in
test_passenger_portal.py::test_concurrent_registration_last_seat is a
best-effort simulation, not a guarantee that SELECT...FOR UPDATE behaves
identically to production. Run against a real Postgres instance
(matching SQLALCHEMY_DATABASE_URI in your .env) before trusting the
concurrency guarantee fully.
"""
import os
import tempfile

import pytest

_db_fd, _db_path = tempfile.mkstemp(suffix=".sqlite3")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["SECRET_KEY"] = "test-secret-key"

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