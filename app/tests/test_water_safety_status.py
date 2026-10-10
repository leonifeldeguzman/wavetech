"""Water-level Safety Status (Admin dashboard + Passenger home) must follow
the Safety Thresholds saved in Admin Settings -- no hardcoded limits."""
import pytest

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service, scheduling_service, settings_service


def _save(**water):
    t = settings_service.get_safety_thresholds()
    t.update(water)
    settings_service.update_safety_thresholds(**t)


def _add_reading(level):
    db.session.add(EnvironmentalReading(source="manual", water_level_m=level))
    db.session.commit()


@pytest.mark.parametrize("level, expected", [
    (10.50, "safe"), (11.5, "safe"), (12.50, "safe"),
    (10.00, "caution"), (10.20, "caution"), (12.51, "caution"),
    (12.8, "caution"), (13.00, "caution"),
    (9.99, "unsafe"), (13.01, "unsafe"),
])
def test_classification_follows_saved_settings(app, level, expected):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        _add_reading(level)
        status, label, _ = monitoring_service.get_current_safety()
        assert status == expected
        assert label == expected.upper()


def test_changing_settings_changes_status_without_code_change(app):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        _add_reading(12.8)
        assert monitoring_service.get_current_safety()[0] == "caution"

        # Admin widens the safe range -> same reading is now Safe.
        _save(water_safe_max_m=13.00)
        assert monitoring_service.get_current_safety()[0] == "safe"

        # Admin tightens the caution range -> same reading is now Unsafe.
        _save(water_safe_max_m=12.50, water_caution_max_m=12.60)
        assert monitoring_service.get_current_safety()[0] == "unsafe"


def test_no_reading_is_pending(app):
    with app.app_context():
        status, label, reading = monitoring_service.get_current_safety()
        assert (status, reading) == ("pending", None)


def test_matches_decision_support_water_classification(app):
    with app.app_context():
        _save(water_safe_min_m=10.50, water_safe_max_m=12.50,
              water_caution_min_m=10.00, water_caution_max_m=13.00)
        for level in (9.9, 10.0, 11.0, 12.8, 13.0, 13.2):
            dss = scheduling_service._classify_water(
                level, scheduling_service._thresholds()).status.lower()
            assert monitoring_service.classify_water_safety(level)[0] == dss


def test_overall_condition_uses_new_statuses(app):
    safe_env = {"status": scheduling_service.STATUS_SAFE}
    assert scheduling_service.get_overall_condition("unsafe", safe_env)["status"] == scheduling_service.STATUS_UNSAFE
    assert scheduling_service.get_overall_condition("caution", safe_env)["status"] == scheduling_service.STATUS_CAUTION
    assert scheduling_service.get_overall_condition("safe", safe_env)["status"] == scheduling_service.STATUS_SAFE
