from datetime import timedelta
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.announcement import Announcement
from app.models.pending_registration import PendingRegistration
from app.models.trip import Trip
from app.services import announcement_service
from app.services.unisms_service import SmsResult
from app.utils.timezone import utc_now_naive

SEND_SMS = "app.services.unisms_service.send_sms"
LOG_ACTION = "app.services.announcement_service.log_action"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sent():
    return SmsResult(success=True, status="sent", error=None, raw_response={})  # ADAPT if SmsResult differs


def _failed():
    return SmsResult(success=False, status="failed", error="boom", raw_response={})


def _make_trip(status="Open"):
    trip = Trip(departure_time=utc_now_naive() + timedelta(hours=2), status=status)
    db.session.add(trip)
    db.session.commit()
    return trip


def _make_reg(trip, number, status="approved", name="Juan Dela Cruz"):
    reg = PendingRegistration(
        trip_id=trip.id,
        full_name=name,
        age=30,
        contact_number=number,
        status=status,
    )
    db.session.add(reg)
    db.session.commit()
    return reg


def _publish(title="Storm warning", content="Trips may be delayed."):
    return announcement_service.create_announcement(
        title=title,
        content=content,
        type_=Announcement.TYPE_SAFETY_ALERT,
        publish_now=True,
    )


def _numbers_sent(mock_send):
    return [call.args[0] for call in mock_send.call_args_list]


@pytest.fixture(autouse=True)
def _sync_sms(app):  # ADAPT: use your app fixture name
    app.config["SMS_DISPATCH_ASYNC"] = False
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_approved_passengers_on_two_open_trips_each_get_one_sms(app):
    with app.app_context():
        t1, t2 = _make_trip("Open"), _make_trip("Boarding")
        _make_reg(t1, "09171111111")
        _make_reg(t2, "09172222222")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish()

        assert sorted(_numbers_sent(mock_send)) == ["09171111111", "09172222222"]


def test_pending_and_rejected_get_none(app):
    with app.app_context():
        trip = _make_trip()
        _make_reg(trip, "09171111111", status="pending")
        _make_reg(trip, "09172222222", status="rejected")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish()

        mock_send.assert_not_called()


@pytest.mark.parametrize("status", ["Cancelled", "Departed"])
def test_cancelled_and_departed_trips_get_none(app, status):
    with app.app_context():
        trip = _make_trip(status)
        _make_reg(trip, "09171111111")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish()

        mock_send.assert_not_called()


def test_same_number_registered_twice_gets_one_sms(app):
    with app.app_context():
        t1, t2 = _make_trip(), _make_trip()
        _make_reg(t1, "09171111111")
        _make_reg(t2, "09171111111", name="Same Person")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish()

        assert mock_send.call_count == 1


def test_publish_then_reactivate_sends_only_once(app):
    with app.app_context():
        trip = _make_trip()
        _make_reg(trip, "09171111111")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            ann = _publish()
            announcement_service.deactivate_announcement(ann)
            announcement_service.activate_announcement(ann)

        assert mock_send.call_count == 1


def test_failed_send_is_counted_in_log_and_does_not_raise(app):
    with app.app_context():
        trip = _make_trip()
        _make_reg(trip, "09171111111")

        with patch(SEND_SMS, return_value=_failed()), patch(LOG_ACTION) as mock_log:
            _publish()  # must not raise

        mock_log.assert_called_once()
        details = mock_log.call_args.kwargs["details"]
        assert "failed=1" in details
        assert "sent=0" in details


def test_zero_recipients_makes_no_calls_and_does_not_crash(app):
    with app.app_context():
        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish()

        mock_send.assert_not_called()


def test_long_message_is_truncated_to_300_chars(app):
    with app.app_context():
        trip = _make_trip()
        _make_reg(trip, "09171111111")

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            _publish(content="x" * 1000)

        message = mock_send.call_args.args[1]
        assert len(message) <= 300
        assert message.endswith("...")


def test_scheduled_announcement_broadcasts_when_due(app):
    with app.app_context():
        trip = _make_trip()
        _make_reg(trip, "09171111111")

        ann = Announcement(
            title="Scheduled alert",
            content="Rough water expected.",
            type=Announcement.TYPE_SAFETY_ALERT,
            status=Announcement.STATUS_INACTIVE,
            scheduled_at=utc_now_naive() - timedelta(minutes=1),
        )
        db.session.add(ann)
        db.session.commit()

        with patch(SEND_SMS, return_value=_sent()) as mock_send:
            published = announcement_service.publish_due_announcements()

        assert published == 1
        assert mock_send.call_count == 1