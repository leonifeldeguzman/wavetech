"""
Tests for app/services/llda_service.py.

These use mocked HTTP responses throughout — none of these tests talk to
the real LLDA service, per the task requirement that automated tests must
not depend on a live external service (and per the research findings,
there currently isn't a real documented LLDA API to depend on anyway).
"""
from unittest.mock import patch, MagicMock

import pytest
import requests

from app.services import llda_service
from app.services.llda_service import (
    LLDANotConfiguredError,
    LLDATimeoutError,
    LLDAConnectionError,
    LLDAHTTPError,
    LLDAParseError,
)


@pytest.fixture()
def configured(app):
    """Run inside an app context with a fake LLDA URL configured."""
    with app.app_context():
        app.config["LLDA_API_URL"] = "https://example-llda-endpoint.test/water-level"
        app.config["LLDA_API_KEY"] = "test-key"
        app.config["LLDA_API_TIMEOUT_SECONDS"] = 5
        yield app


def _mock_response(status_code=200, json_data=None, raise_on_json=False):
    resp = MagicMock()
    resp.status_code = status_code
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data or {}
    return resp


class TestNotConfigured:
    def test_raises_when_no_url_configured(self, app):
        with app.app_context():
            app.config["LLDA_API_URL"] = ""
            with pytest.raises(LLDANotConfiguredError):
                llda_service.fetch_water_level()

    def test_does_not_make_any_http_call_when_not_configured(self, app):
        with app.app_context():
            app.config["LLDA_API_URL"] = ""
            with patch("app.services.llda_service.requests.get") as mocked_get:
                with pytest.raises(LLDANotConfiguredError):
                    llda_service.fetch_water_level()
                mocked_get.assert_not_called()


class TestSuccessfulRequest:
    def test_parses_valid_response(self, configured):
        payload = {
            "station": "Central Bay, Cardona, Rizal",
            "water_level_m": 12.34,
            "unit": "m",
            "recorded_at": "2026-09-07T19:30:00+08:00",
        }
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(200, payload)):
            reading = llda_service.fetch_water_level()

        assert reading.source == "llda"
        assert reading.water_level_m == 12.34
        assert reading.station == "Central Bay, Cardona, Rizal"
        assert reading.recorded_at is not None
        assert reading.retrieved_at is not None

    def test_sends_auth_header_when_key_present(self, configured):
        payload = {"station": "West Bay", "water_level_m": 11.9}
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(200, payload)) as mocked_get:
            llda_service.fetch_water_level()

        _, kwargs = mocked_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"


class TestInvalidResponse:
    def test_missing_water_level_field_raises_parse_error(self, configured):
        payload = {"station": "East Bay"}
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(200, payload)):
            with pytest.raises(LLDAParseError):
                llda_service.fetch_water_level()

    def test_non_numeric_water_level_raises_parse_error(self, configured):
        payload = {"station": "East Bay", "water_level_m": "not-a-number"}
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(200, payload)):
            with pytest.raises(LLDAParseError):
                llda_service.fetch_water_level()

    def test_non_json_body_raises_parse_error(self, configured):
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(200, raise_on_json=True)):
            with pytest.raises(LLDAParseError):
                llda_service.fetch_water_level()


class TestTimeoutAndConnectionErrors:
    def test_timeout_raises_llda_timeout_error(self, configured):
        with patch("app.services.llda_service.requests.get", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(LLDATimeoutError):
                llda_service.fetch_water_level()

    def test_connection_error_raises_llda_connection_error(self, configured):
        with patch("app.services.llda_service.requests.get", side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(LLDAConnectionError):
                llda_service.fetch_water_level()

    def test_other_request_exception_raises_llda_connection_error(self, configured):
        with patch("app.services.llda_service.requests.get", side_effect=requests.exceptions.RequestException("boom")):
            with pytest.raises(LLDAConnectionError):
                llda_service.fetch_water_level()


class TestHttpErrors:
    def test_non_200_status_raises_http_error(self, configured):
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(503)):
            with pytest.raises(LLDAHTTPError) as excinfo:
                llda_service.fetch_water_level()
        assert excinfo.value.status_code == 503

    def test_404_raises_http_error(self, configured):
        with patch("app.services.llda_service.requests.get", return_value=_mock_response(404)):
            with pytest.raises(LLDAHTTPError) as excinfo:
                llda_service.fetch_water_level()
        assert excinfo.value.status_code == 404