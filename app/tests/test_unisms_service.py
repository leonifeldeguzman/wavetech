"""
Standalone tests for app/services/unisms_service.py.

These test the service module directly, independent of any route,
since unisms_service is not currently wired into
_execute_registration() — routes.py still calls semaphore_service.
When/if you switch routes.py over to unisms_service, the existing
app/tests/test_registration_sms.py pattern (monkeypatching send_sms at
the route level) applies unchanged — just point monkeypatch.setattr at
"app.services.unisms_service.send_sms" instead.

Never hits the real UniSMS API — `requests.post` is monkeypatched in
every test below.
"""
import pytest

from app.services.unisms_service import SmsResult


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {"message": {"status": "sent"}}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json_data


def test_send_sms_skips_when_no_number(app):
    from app.services.unisms_service import send_sms
    with app.app_context():
        result = send_sms(None, "hello")
        assert result.success is False
        assert result.status == "skipped_no_number"


def test_send_sms_skips_when_no_api_key(app, monkeypatch):
    from app.services.unisms_service import send_sms
    with app.app_context():
        app.config["UNISMS_API_KEY"] = ""
        result = send_sms("09171234567", "hello")
        assert result.success is False
        assert result.status == "skipped_no_config"


def test_send_sms_success(app, monkeypatch):
    from app.services import unisms_service

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        # Confirm the request is shaped the way UniSMS's docs specify:
        # Basic Auth with the key as username/empty password, and a
        # plain {"recipient", "content"} JSON body — not an X-API-Key
        # header or a differently-named field, which other providers use.
        assert json == {
            "recipient": "09171234567",
            "content": "hello",
            "sender_id": "Unisoft",
        }
        assert auth.username == "test-key"
        assert auth.password == ""
        return _FakeResponse(200, {"message": {"reference_id": "msg_abc", "status": "sent"}})

    monkeypatch.setattr("app.services.unisms_service.requests.post", fake_post)

    with app.app_context():
        app.config["UNISMS_API_KEY"] = "test-key"
        app.config["UNISMS_SENDER_ID"] = "Unisoft"
        app.config["UNISMS_API_URL"] = "https://unismsapi.com/api/sms"
        result = unisms_service.send_sms("09171234567", "hello")

    assert result.success is True
    assert result.status == "sent"
    assert result.raw_response["message"]["reference_id"] == "msg_abc"


def test_send_sms_failure_response(app, monkeypatch):
    from app.services import unisms_service

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        return _FakeResponse(403, {"error": "insufficient credits"})

    monkeypatch.setattr("app.services.unisms_service.requests.post", fake_post)

    with app.app_context():
        app.config["UNISMS_API_KEY"] = "test-key"
        app.config["UNISMS_API_URL"] = "https://unismsapi.com/api/sms"
        result = unisms_service.send_sms("09171234567", "hello")

    assert result.success is False
    assert result.status == "failed"
    assert result.error is not None


def test_send_sms_network_error(app, monkeypatch):
    from app.services import unisms_service
    import requests

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        raise requests.ConnectionError("simulated network failure")

    monkeypatch.setattr("app.services.unisms_service.requests.post", fake_post)

    with app.app_context():
        app.config["UNISMS_API_KEY"] = "test-key"
        app.config["UNISMS_API_URL"] = "https://unismsapi.com/api/sms"
        result = unisms_service.send_sms("09171234567", "hello")

    assert result.success is False
    assert result.status == "failed"
    assert "simulated network failure" in result.error