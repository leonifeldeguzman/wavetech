from app.blueprints.monitoring.routes import _age_info
from app.services import scheduling_service
from datetime import datetime, timedelta, timezone


def _utc_ago(**kw):
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**kw)


def test_age_label_and_stale_flag():
    info = _age_info(_utc_ago(hours=9), timedelta(hours=3))
    assert info["text"] == "9 hours ago" and info["stale"] is True
    assert _age_info(_utc_ago(minutes=20), timedelta(hours=3))["stale"] is False


def test_missing_items_are_listed(app):
    with app.app_context():
        a = scheduling_service.evaluate_conditions(None, None)
    assert a.missing == ("wind speed", "weather condition", "today's water level")