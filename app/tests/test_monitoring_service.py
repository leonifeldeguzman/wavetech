"""
Tests for app/services/monitoring_service.py — the live/cached/unavailable
fallback flow described in the SRS failure-handling diagram, and the
guarantee that a scheduling recommendation can never be based on stale or
missing environmental data.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.environmental_reading import EnvironmentalReading
from app.services import monitoring_service
from app.services.llda_service import LLDAReading, LLDATimeoutError, LLDANotConfiguredError


def _fake_live_reading():
    return LLDAReading(
        source="llda",
        station="Central Bay, Cardona, Rizal",
        water_level_m=12.1,
        unit="m",
        recorded_at=datetime.now(timezone.utc),
        retrieved_at=datetime.now(timezone.utc),
        raw={},
    )


class TestLiveSuccess:
    def test_successful_fetch_is_saved_and_marked_live(self, app):
        with app.app_context():
            with patch("app.services.monitoring_service.llda_service.fetch_water_level", return_value=_fake_live_reading()):
                conditions = monitoring_service.get_llda_conditions()

            assert conditions["status"] == "live"
            assert conditions["reading"] is not None
            assert conditions["reading"].water_level_m == 12.1

            stored = EnvironmentalReading.query.filter_by(source="llda").all()
            assert len(stored) == 1
            assert stored[0].water_level_m == 12.1

    def test_live_reading_does_not_block_recommendation(self, app):
        with app.app_context():
            with patch("app.services.monitoring_service.llda_service.fetch_water_level", return_value=_fake_live_reading()):
                conditions = monitoring_service.get_llda_conditions()
            assert monitoring_service.no_data_blocks_recommendation(conditions) is False


class TestCachedFallback:
    def test_failure_with_prior_reading_falls_back_to_cached(self, app):
        with app.app_context():
            db.session.add(
                EnvironmentalReading(
                    source="llda",
                    location_label="West Bay",
                    water_level_m=11.8,
                    recorded_at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None),
                    retrieved_at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None),
                )
            )
            db.session.commit()

            with patch("app.services.monitoring_service.llda_service.fetch_water_level", side_effect=LLDATimeoutError("timed out")):
                conditions = monitoring_service.get_llda_conditions()

            assert conditions["status"] == "cached"
            assert conditions["reading"].water_level_m == 11.8
            assert conditions["message"]

    def test_cached_reading_still_blocks_recommendation(self, app):
        with app.app_context():
            db.session.add(
                EnvironmentalReading(
                    source="llda",
                    location_label="West Bay",
                    water_level_m=11.8,
                    recorded_at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None),
                    retrieved_at=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None),
                )
            )
            db.session.commit()

            with patch("app.services.monitoring_service.llda_service.fetch_water_level", side_effect=LLDATimeoutError("timed out")):
                conditions = monitoring_service.get_llda_conditions()

            assert monitoring_service.no_data_blocks_recommendation(conditions) is True

    def test_cached_fallback_ignores_manual_readings(self, app):
        """Cached fallback should only ever surface a previous LLDA reading,
        never a manually-encoded one, so operators aren't misled about the
        data's provenance."""
        with app.app_context():
            db.session.add(EnvironmentalReading(source="manual", location_label="Cabuyao Terminal", water_level_m=9.99))
            db.session.commit()

            with patch("app.services.monitoring_service.llda_service.fetch_water_level", side_effect=LLDANotConfiguredError("no url")):
                conditions = monitoring_service.get_llda_conditions()

            assert conditions["status"] == "unavailable"


class TestUnavailable:
    def test_failure_with_no_prior_reading_is_unavailable(self, app):
        with app.app_context():
            with patch("app.services.monitoring_service.llda_service.fetch_water_level", side_effect=LLDANotConfiguredError("no url configured")):
                conditions = monitoring_service.get_llda_conditions()

            assert conditions["status"] == "unavailable"
            assert conditions["reading"] is None
            assert "Manual verification" in conditions["message"]

    def test_unavailable_blocks_recommendation(self, app):
        with app.app_context():
            with patch("app.services.monitoring_service.llda_service.fetch_water_level", side_effect=LLDANotConfiguredError("no url configured")):
                conditions = monitoring_service.get_llda_conditions()

            assert monitoring_service.no_data_blocks_recommendation(conditions) is True

def test_fresh_windy_reading_avoids_duplicate_api_request_and_record(app):
    """A fresh Windy row is reused inside the refresh window."""

    with app.app_context():
        now = datetime.now(timezone.utc)
        stored = EnvironmentalReading(
            source="windy",
            wind_speed_kmh=12.0,
            wind_direction="NE",
            weather_condition="Sunny",
            temperature_c=28.0,
            recorded_at=now.replace(tzinfo=None),
            retrieved_at=now.replace(tzinfo=None),
        )
        db.session.add(stored)
        db.session.commit()

        with patch(
            "app.services.monitoring_service.windy_service.fetch_conditions"
        ) as mocked_fetch:
            conditions = monitoring_service.get_windy_conditions()

        mocked_fetch.assert_not_called()
        assert conditions["status"] == "live"
        assert conditions["reading"].id == stored.id
        assert EnvironmentalReading.query.filter_by(source="windy").count() == 1
