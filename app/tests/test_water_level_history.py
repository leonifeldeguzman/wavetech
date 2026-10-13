"""Tests for historical_baseline_service.get_water_level_history().

The Trends chart must never invent data: days without a value stay None,
and the historical series and the recorded series are kept separate.
"""
from datetime import date, datetime

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.models.historical_water_level import HistoricalWaterLevel
from app.services import historical_baseline_service as svc
from app.utils.ph_clock import ph_today


def _hist(day, level):
    # ADAPT: if the model needs more required fields, add them here.
    db.session.add(HistoricalWaterLevel(date=day, level_m=level, unit="m"))


def _reading(source, level, retrieved_at):
    db.session.add(EnvironmentalReading(
        source=source, water_level_m=level, retrieved_at=retrieved_at))


def test_returns_none_when_no_historical_data(app):
    assert svc.get_water_level_history(date(2025, 1, 1), date(2025, 1, 5)) is None


def test_missing_days_stay_none_and_are_not_filled(app):
    _hist(date(2025, 1, 1), 12.0)
    _hist(date(2025, 1, 3), 12.2)
    db.session.commit()

    r = svc.get_water_level_history(date(2025, 1, 1), date(2025, 1, 4))

    assert r["labels"] == ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]
    assert r["historical"] == [12.0, None, 12.2, None]
    assert r["n_historical"] == 2
    assert r["recorded"] == [None, None, None, None]


def test_recorded_uses_ph_day_newest_wins(app):
    _hist(date(2025, 6, 1), 12.0)
    # 16:30 UTC on Jun 10 is 00:30 PH on Jun 11.
    _reading("manual", 12.1, datetime(2025, 6, 10, 16, 30))
    # Two readings the same PH day (Jun 12): the later one wins.
    _reading("manual", 12.3, datetime(2025, 6, 12, 1, 0))
    _reading("manual", 12.4, datetime(2025, 6, 12, 5, 0))
    db.session.commit()

    r = svc.get_water_level_history(date(2025, 6, 10), date(2025, 6, 12))

    assert r["labels"] == ["2025-06-10", "2025-06-11", "2025-06-12"]
    assert r["recorded"] == [None, 12.1, 12.4]
    assert r["n_recorded"] == 2


def test_only_manual_and_llda_readings_with_a_level_are_recorded(app):
    _hist(date(2025, 6, 1), 12.0)
    _reading("open_meteo", 99.0, datetime(2025, 6, 10, 2, 0))
    _reading("windy", 98.0, datetime(2025, 6, 10, 3, 0))
    _reading("manual", None, datetime(2025, 6, 10, 4, 0))
    _reading("llda", 12.6, datetime(2025, 6, 10, 5, 0))
    db.session.commit()

    r = svc.get_water_level_history(date(2025, 6, 10), date(2025, 6, 10))

    assert r["recorded"] == [12.6]


def test_end_is_clamped_to_today(app):
    _hist(date(2025, 1, 1), 12.0)
    db.session.commit()

    r = svc.get_water_level_history(date(2025, 1, 1), date(2099, 1, 1))

    assert r["end"] == ph_today()


def test_default_range_starts_at_first_historical_day(app):
    _hist(date(2023, 1, 1), 11.0)
    _hist(date(2025, 12, 31), 12.0)
    db.session.commit()

    r = svc.get_water_level_history()

    assert r["start"] == date(2023, 1, 1)
    assert r["first"] == date(2023, 1, 1)
    assert r["last"] == date(2025, 12, 31)