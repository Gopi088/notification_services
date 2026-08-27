"""Unit tests for the Twilio providers (SMS + WhatsApp) - all HTTP mocked.

Every env override uses `monkeypatch` so real credentials from `.env` never
leak into the test run and each test restores the environment afterwards.
"""
import json
from unittest.mock import patch

import pytest
import requests

from app.providers.base import ProviderConfigError, ProviderError
from app.providers.twilio_provider import (
    TwilioSMSProvider,
    TwilioWhatsAppProvider,
    _content_variables,
)


@pytest.fixture(autouse=True)
def twilio_env(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000001")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-secret")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+17372508034")
    monkeypatch.setenv("TWILIO_WHATSAPP_CONTENT_SID", "HXfe5ab5f00277942d4d4200328b4d403c")
    monkeypatch.setenv("TWILIO_SMS_TEMPLATE", "sms_appointment_reminders")
    monkeypatch.setenv("AZURE_DEFAULT_COUNTRY_CODE", "91")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_response(status_code: int, body: dict):
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps(body).encode()
    return resp


_OK = {"sid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "status": "queued"}


def test_sms_success():
    captured = {}

    def fake_post(url, auth=None, data=None, timeout=None, **kw):
        captured["url"] = url
        captured["auth"] = auth
        captured["data"] = data
        captured["timeout"] = timeout
        return _fake_response(201, _OK)

    provider = TwilioSMSProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send("9887270348", "Your OTP is 482913")

    assert "ACtest0000000000000000000000000001" in captured["url"]
    assert captured["auth"] == ("ACtest0000000000000000000000000001", "tok-secret")
    assert captured["data"]["To"] == "+919887270348"
    assert captured["data"]["From"] == "+17372508034"
    assert captured["data"]["Body"] == "Your OTP is 482913"
    assert captured["timeout"] == 30
    assert result.provider_name == "twilio_sms"
    assert result.provider_message_id == "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert result.status == "submitted"


def test_sms_number_normalized_from_plus():
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    provider = TwilioSMSProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        provider.send("+919887270348", "hi")
    assert captured["data"]["To"] == "+919887270348"


def test_sms_missing_sid(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hi")


def test_sms_missing_token(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hi")


def test_sms_missing_from(monkeypatch):
    monkeypatch.setenv("TWILIO_FROM", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hi")


def test_whatsapp_text_success():
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    provider = TwilioWhatsAppProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send("9887270348", "hello")

    assert captured["data"]["To"] == "whatsapp:+919887270348"
    assert captured["data"]["From"] == "whatsapp:+17372508034"
    assert captured["data"]["Body"] == "hello"
    assert result.provider_message_id == "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def test_whatsapp_template_success():
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    provider = TwilioWhatsAppProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send_with_template(
            "9887270348", "ignored", "test_template",
            template_params={"name": "Rahul"},
        )

    assert captured["data"]["To"] == "whatsapp:+919887270348"
    assert captured["data"]["From"] == "whatsapp:+17372508034"
    assert captured["data"]["ContentSid"] == "HXfe5ab5f00277942d4d4200328b4d403c"
    assert json.loads(captured["data"]["ContentVariables"]) == {"1": "Rahul"}
    assert "Body" not in captured["data"]
    assert result.provider_message_id == "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def test_whatsapp_template_named_mapping(monkeypatch):
    monkeypatch.setenv("TWILIO_WHATSAPP_TEMPLATES", "reminder=HXreminder123;welcome=HXwelcome456")
    from app.config import get_settings

    get_settings.cache_clear()
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    provider = TwilioWhatsAppProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        provider.send_with_template("9887270348", "x", "reminder")
    assert captured["data"]["ContentSid"] == "HXreminder123"


def test_whatsapp_template_no_sid_for_name(monkeypatch):
    monkeypatch.setenv("TWILIO_WHATSAPP_CONTENT_SID", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioWhatsAppProvider()
    with pytest.raises(ProviderConfigError):
        provider.send_with_template("9887270348", "x", "unknown_template")


def test_whatsapp_template_no_default_sid(monkeypatch):
    monkeypatch.setenv("TWILIO_WHATSAPP_CONTENT_SID", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioWhatsAppProvider()
    with pytest.raises(ProviderConfigError):
        provider.send_template("9887270348", "")


def test_whatsapp_missing_sender(monkeypatch):
    monkeypatch.setenv("TWILIO_FROM", "")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provider = TwilioWhatsAppProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hi")


def test_network_error_retryable():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        side_effect=requests.ConnectionError("connection refused"),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert excinfo.value.retryable is True
    assert "network" in str(excinfo.value).lower()


def test_http_401_auth_error():
    provider = TwilioWhatsAppProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(401, {"code": 20003, "message": "auth"}),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert "authentication" in str(excinfo.value).lower()
    assert excinfo.value.retryable is False


def test_http_429_rate_limited_retryable():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(429, {"message": "rate"}),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert excinfo.value.retryable is True
    assert "429" in str(excinfo.value)


def test_http_500_retryable():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(500, {"message": "boom"}),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert excinfo.value.retryable is True


def test_http_400_not_retryable():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(400, {"code": 21211, "message": "invalid number"}),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert excinfo.value.retryable is False
    assert "400" in str(excinfo.value)


def test_no_sid_in_response():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(201, {"status": "queued"}),
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert "no message sid" in str(excinfo.value)


def test_invalid_json_response():
    resp = requests.Response()
    resp.status_code = 200
    resp._content = b"not-json"
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post", return_value=resp,
    ):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert "invalid response" in str(excinfo.value)


def test_secret_not_exposed_in_errors():
    provider = TwilioSMSProvider()
    with patch(
        "app.providers.twilio_provider.requests.post",
        return_value=_fake_response(500, {"message": "boom"}),
    ):
        try:
            provider.send("9887270348", "hi")
        except ProviderError as exc:
            assert "tok-secret" not in str(exc)


def test_mock_mode_short_circuit(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    result = TwilioSMSProvider().send("9887270348", "hi")
    assert result.provider_name == "twilio_sms"
    assert result.provider_message_id.startswith("mock-")


def test_content_variables_named_to_positional():
    assert _content_variables({"name": "Rahul", "date": "26 Aug"}) == json.dumps(
        {"1": "Rahul", "2": "26 Aug"}
    )


def test_content_variables_numeric_keys_preserved():
    assert _content_variables({"1": "a", "2": "b"}) == json.dumps({"1": "a", "2": "b"})


def test_content_variables_empty():
    assert _content_variables(None) is None
    assert _content_variables({}) is None


def test_sms_send_delivery_plain():
    provider = TwilioSMSProvider()
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send_delivery({"recipient": "9887270348", "message": "hi"})
    assert captured["data"]["Body"] == "hi"
    assert result.provider_name == "twilio_sms"


def test_sms_trial_fallback_to_template():
    """Free-form SMS rejected (572006) retries with TWILIO_SMS_TEMPLATE."""
    calls = []

    def fake_post(url, data=None, **kw):
        calls.append(dict(data))
        if len(calls) == 1:
            return _fake_response(400, {"code": 572006, "message": "Invalid template name."})
        return _fake_response(201, _OK)

    provider = TwilioSMSProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send("9887270348", "custom free text")

    assert len(calls) == 2
    assert calls[0]["Body"] == "custom free text"
    assert calls[1]["Body"] == "sms_appointment_reminders"
    assert result.provider_message_id == "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def test_sms_trial_no_fallback_without_template(monkeypatch):
    monkeypatch.setenv("TWILIO_SMS_TEMPLATE", "")
    from app.config import get_settings

    get_settings.cache_clear()

    def fake_post(url, data=None, **kw):
        return _fake_response(400, {"code": 572006, "message": "Invalid template name."})

    provider = TwilioSMSProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "custom free text")
    assert "572006" in str(excinfo.value)


def test_whatsapp_trial_fallback_to_template():
    """Free-form WhatsApp rejected (21654) falls back to the ContentSid template."""
    calls = []

    def fake_post(url, data=None, **kw):
        calls.append(dict(data))
        if len(calls) == 1:
            return _fake_response(400, {"code": 21654, "message": "ContentSid Required"})
        return _fake_response(201, _OK)

    provider = TwilioWhatsAppProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        result = provider.send("9887270348", "custom free text")

    assert len(calls) == 2
    assert "Body" in calls[0]
    assert calls[1]["ContentSid"] == "HXfe5ab5f00277942d4d4200328b4d403c"
    assert calls[1]["To"] == "whatsapp:+919887270348"
    assert result.provider_message_id == "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def test_whatsapp_no_fallback_on_other_error():
    """A non-template error (e.g. invalid number) must NOT trigger a fallback."""
    def fake_post(url, data=None, **kw):
        return _fake_response(400, {"code": 21211, "message": "invalid number"})

    provider = TwilioWhatsAppProvider()
    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        with pytest.raises(ProviderError) as excinfo:
            provider.send("9887270348", "hi")
    assert "21211" in str(excinfo.value)


def test_whatsapp_send_delivery_template():
    provider = TwilioWhatsAppProvider()
    captured = {}

    def fake_post(url, data=None, **kw):
        captured["data"] = data
        return _fake_response(201, _OK)

    with patch("app.providers.twilio_provider.requests.post", side_effect=fake_post):
        provider.send_delivery(
            {
                "recipient": "9887270348",
                "message": "ignored",
                "template": {
                    "id": "test_template",
                    "params": [{"name": "1", "value": "Rahul"}],
                },
            }
        )
    assert captured["data"]["ContentSid"] == "HXfe5ab5f00277942d4d4200328b4d403c"
    assert json.loads(captured["data"]["ContentVariables"]) == {"1": "Rahul"}
