"""Unit tests for the Azure providers (SMS, Email, WhatsApp) - all mocked."""
from unittest.mock import MagicMock, patch

import pytest

from app.providers.azure_provider import (
    AzureEmailProvider,
    AzureSMSProvider,
    AzureWhatsAppProvider,
    _normalize_phone,
)
from app.providers.base import ProviderConfigError, ProviderResult


@pytest.fixture(autouse=True)
def azure_env(monkeypatch):
    import os

    os.environ["MOCK_MODE"] = "false"
    os.environ["STORAGE_BACKEND"] = "sqlite"
    os.environ["AZURE_SMS_FROM"] = "+919812345678"
    os.environ["AZURE_EMAIL_FROM"] = "noreply@example.com"
    os.environ["COMMUNICATION_SERVICES_CONNECTION_STRING"] = "endpoint=https://x.communication.azure.com/;accesskey=abc"
    os.environ["WHATSAPP_CHANNEL_ID"] = "chan-123"
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_normalize_phone_variants():
    assert _normalize_phone("9887270348", "91") == "+919887270348"
    assert _normalize_phone("0-98872-70348", "91") == "+919887270348"
    assert _normalize_phone("919887270348", "91") == "+919887270348"
    assert _normalize_phone("+919887270348", "91") == "+919887270348"


def test_azure_sms_success():
    provider = AzureSMSProvider()
    sms_client = MagicMock()
    result = MagicMock()
    result.successful = True
    result.to = "+919887270348"
    result.message_id = "azure-sms-id"
    sms_client.send.return_value = [result]
    with patch("azure.communication.sms.SmsClient.from_connection_string", return_value=sms_client):
        r = provider.send("9887270348", "hello")
    assert r.provider_name == "azure_sms"
    assert r.provider_message_id == "azure-sms-id"


def test_azure_sms_failure():
    provider = AzureSMSProvider()
    sms_client = MagicMock()
    result = MagicMock()
    result.successful = False
    result.to = "+919887270348"
    result.error_message = "invalid number"
    sms_client.send.return_value = [result]
    with patch("azure.communication.sms.SmsClient.from_connection_string", return_value=sms_client):
        with pytest.raises(Exception):
            provider.send("9887270348", "hello")


def test_azure_sms_missing_from(monkeypatch):
    import os

    os.environ["AZURE_SMS_FROM"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = AzureSMSProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hello")


def test_azure_email_success():
    provider = AzureEmailProvider()
    email_client = MagicMock()
    poller = MagicMock()
    poller.result.return_value = {"message_id": "azure-email-id"}
    email_client.begin_send.return_value = poller
    with patch("azure.communication.email.EmailClient.from_connection_string", return_value=email_client):
        r = provider.send("a@b.com", "hello", subject="Subj")
    assert r.provider_message_id == "azure-email-id"


def test_azure_email_missing_from(monkeypatch):
    import os

    os.environ["AZURE_EMAIL_FROM"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = AzureEmailProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("a@b.com", "hello")


def test_azure_email_attachment_http_rejected():
    provider = AzureEmailProvider()
    with pytest.raises(Exception) as excinfo:
        provider._fetch_as_base64("http://example.com/file.pdf")
    assert "https" in str(excinfo.value)


def test_azure_email_attachment_localhost_rejected():
    provider = AzureEmailProvider()
    with pytest.raises(Exception) as excinfo:
        provider._fetch_as_base64("https://localhost/file.pdf")
    assert "localhost" in str(excinfo.value)


def test_azure_email_attachment_private_ip_rejected():
    provider = AzureEmailProvider()
    with pytest.raises(Exception) as excinfo:
        provider._fetch_as_base64("https://192.168.1.1/file.pdf")
    assert "blocked" in str(excinfo.value)


def test_azure_whatsapp_missing_channel(monkeypatch):
    import os

    os.environ["WHATSAPP_CHANNEL_ID"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = AzureWhatsAppProvider()
    with pytest.raises(ProviderConfigError):
        provider.send("9887270348", "hello")


def test_azure_whatsapp_missing_connection_string(monkeypatch):
    import os

    os.environ["COMMUNICATION_SERVICES_CONNECTION_STRING"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    provider = AzureWhatsAppProvider()
    with pytest.raises(ProviderConfigError):
        provider.send_template("9887270348", "tpl")


def test_azure_whatsapp_missing_template_name():
    provider = AzureWhatsAppProvider()
    from app.config import get_settings

    settings = get_settings()
    assert settings.whatsapp_channel_id  # sanity
    with pytest.raises(ProviderConfigError):
        provider.send_template("9887270348", "")
