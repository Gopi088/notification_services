"""Tests for the Vonage SMS provider (mocked SDK)."""
from unittest.mock import MagicMock, patch

import pytest

from app.providers.base import ProviderConfigError
from app.providers.vonage_provider import VonageSMSProvider


@pytest.fixture(autouse=True)
def vonage_env(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("VONAGE_API_KEY", "test-key")
    monkeypatch.setenv("VONAGE_API_SECRET", "test-secret")
    monkeypatch.setenv("VONAGE_SMS_FROM", "Vonage APIs")
    monkeypatch.setenv("AZURE_DEFAULT_COUNTRY_CODE", "91")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_response(message_uuid="uuid-1"):
    resp = MagicMock()
    resp.message_uuid = message_uuid
    return resp


def test_sms_success():
    provider = VonageSMSProvider()
    with patch("vonage_messages.Messages.send", return_value=_fake_response()) as fake:
        with patch("vonage.Vonage") as mock_vonage:
            mock_vonage.return_value.messages = MagicMock()
            mock_vonage.return_value.messages.send.return_value = _fake_response()
            from app.config import get_settings

            result = provider.send("9887270348", "hello")
    assert result.provider_name == "vonage_sms"
    assert result.provider_message_id == "uuid-1"


def test_sms_missing_api_key(monkeypatch):
    import os

    os.environ["VONAGE_API_KEY"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = VonageSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hello")


def test_sms_missing_secret(monkeypatch):
    import os

    os.environ["VONAGE_API_SECRET"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = VonageSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hello")


def test_sms_missing_from(monkeypatch):
    import os

    os.environ["VONAGE_SMS_FROM"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = VonageSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hello")


def test_sms_number_normalized():
    """Vonage gets digits without '+'."""
    provider = VonageSMSProvider()
    captured = {}

    class FakeMessages:
        def send(self, sms):
            captured["sms"] = sms
            return _fake_response()

    with patch("vonage.Vonage") as mock_vonage:
        mock_vonage.return_value.messages = FakeMessages()
        provider.send("+919887270348", "hello")
    assert captured["sms"].to == "919887270348"
    assert captured["sms"].from_ == "Vonage APIs"
    assert captured["sms"].text == "hello"


def test_sms_sdk_error():
    provider = VonageSMSProvider()
    with patch("vonage.Vonage") as mock_vonage:
        mock_vonage.return_value.messages.send.side_effect = RuntimeError("API down")
        with pytest.raises(Exception) as excinfo:
            provider.send("9887270348", "hello")
    assert "Vonage" in str(excinfo.value)


def test_sms_no_message_id():
    provider = VonageSMSProvider()
    resp = MagicMock()
    del resp.message_uuid  # no attribute
    resp.get.return_value = None
    resp.__bool__ = lambda self: True
    with patch("vonage.Vonage") as mock_vonage:
        mock_vonage.return_value.messages.send.return_value = resp
        with pytest.raises(Exception):
            provider.send("9887270348", "hello")
