"""Deeper Azure provider tests: WhatsApp text/template, email delivery/attachments."""
from unittest.mock import MagicMock, patch

import pytest

from app.providers.azure_provider import AzureEmailProvider, AzureWhatsAppProvider
from app.providers.base import ProviderError


@pytest.fixture(autouse=True)
def azure_env(monkeypatch):
    import os

    os.environ["MOCK_MODE"] = "false"
    os.environ["STORAGE_BACKEND"] = "sqlite"
    os.environ["AZURE_SMS_FROM"] = "+919812345678"
    os.environ["AZURE_EMAIL_FROM"] = "noreply@example.com"
    os.environ["COMMUNICATION_SERVICES_CONNECTION_STRING"] = "endpoint=https://x.communication.azure.com/;accesskey=abc"
    os.environ["WHATSAPP_CHANNEL_ID"] = "chan-123"
    os.environ["WHATSAPP_TEMPLATE_NAME"] = "test_template"
    os.environ["WHATSAPP_TEMPLATE_LANGUAGE"] = "en"
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_messages_client():
    client = MagicMock()
    resp = MagicMock()
    receipt = MagicMock()
    receipt.message_id = "wa-msg-1"
    receipt.to = "+919887270348"
    receipt.error = None
    resp.receipts = [receipt]
    client.send.return_value = resp
    return client


def test_azure_whatsapp_text_send():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send("9887270348", "hello")
    assert r.provider_name == "azure_whatsapp"
    assert r.provider_message_id == "wa-msg-1"


def test_azure_whatsapp_receipt_error():
    provider = AzureWhatsAppProvider()
    client = MagicMock()
    resp = MagicMock()
    receipt = MagicMock()
    receipt.error = "channel invalid"
    resp.receipts = [receipt]
    client.send.return_value = resp
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        with pytest.raises(ProviderError):
            provider.send("9887270348", "hello")


def test_azure_whatsapp_no_receipts():
    provider = AzureWhatsAppProvider()
    client = MagicMock()
    resp = MagicMock()
    resp.receipts = []
    client.send.return_value = resp
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        with pytest.raises(ProviderError):
            provider.send("9887270348", "hello")


def test_azure_whatsapp_template_send_no_params():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send_template("9887270348", "test_template", "en")
    assert r.provider_message_id == "wa-msg-1"


def test_azure_whatsapp_template_send_with_params():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send_template("9887270348", "test_template", "en",
                                   {"name": "Rahul", "date": "2026-08-24"})
    assert r.provider_message_id == "wa-msg-1"


def test_azure_whatsapp_send_with_template_alias():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send_with_template("9887270348", "ignored", "test_template", "en")
    assert r.provider_message_id == "wa-msg-1"


def test_azure_whatsapp_send_delivery_with_template():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    payload = {
        "recipient": "+919887270348",
        "template": {"id": "test_template", "language": "en",
                     "params": [{"name": "name", "value": "Rahul"}]},
    }
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send_delivery(payload)
    assert r.provider_message_id == "wa-msg-1"


def test_azure_whatsapp_send_delivery_text():
    provider = AzureWhatsAppProvider()
    client = _fake_messages_client()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string", return_value=client):
        r = provider.send_delivery({"recipient": "+919887270348", "message": "hi"})
    assert r.provider_message_id == "wa-msg-1"


def test_azure_email_send_delivery_full():
    provider = AzureEmailProvider()
    email_client = MagicMock()
    poller = MagicMock()
    poller.result.return_value = {"message_id": "email-id-1"}
    email_client.begin_send.return_value = poller
    payload = {
        "recipient": "a@b.com", "subject": "S", "message": "M",
        "html": "<p>M</p>", "cc": ["c@b.com"], "bcc": ["d@b.com"],
        "replyTo": "r@b.com",
    }
    with patch("azure.communication.email.EmailClient.from_connection_string", return_value=email_client):
        r = provider.send_delivery(payload)
    assert r.provider_message_id == "email-id-1"


def test_azure_email_template_send():
    provider = AzureEmailProvider()
    email_client = MagicMock()
    poller = MagicMock()
    poller.result.return_value = {"message_id": "email-id-2"}
    email_client.begin_send.return_value = poller
    # Use the default email template (renders fine).
    with patch("azure.communication.email.EmailClient.from_connection_string", return_value=email_client):
        r = provider.send_with_template("a@b.com", "hello", "default")
    assert r.provider_message_id == "email-id-2"


def test_azure_email_template_missing_raises():
    provider = AzureEmailProvider()
    with pytest.raises(ProviderError):
        provider.send_with_template("a@b.com", "hello", "nonexistent-template")


def test_azure_email_build_attachments_base64():
    provider = AzureEmailProvider()
    built = provider._build_attachments([
        {"name": "f.txt", "type": "text/plain", "content_base64": "aGVsbG8="}
    ])
    assert built[0]["name"] == "f.txt"
    assert built[0]["contentInBase64"] == "aGVsbG8="


def test_azure_email_build_attachments_no_source():
    provider = AzureEmailProvider()
    with pytest.raises(ProviderError):
        provider._build_attachments([{"name": "f.txt", "type": "text/plain"}])


def test_azure_email_fetch_as_base64_success():
    provider = AzureEmailProvider()
    resp = MagicMock()
    resp.is_redirect = False
    resp.status_code = 200
    resp.headers = {"content-length": "5"}
    resp.iter_bytes.return_value = [b"hello"]
    dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=dns), \
         patch("httpx.stream", return_value=MagicMock(__enter__=MagicMock(return_value=resp))):
        b64 = provider._fetch_as_base64("https://example.com/f.txt")
    assert b64 == "aGVsbG8="


def test_azure_email_fetch_redirect_rejected():
    provider = AzureEmailProvider()
    resp = MagicMock()
    resp.is_redirect = True
    dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=dns), \
         patch("httpx.stream", return_value=MagicMock(__enter__=MagicMock(return_value=resp))):
        with pytest.raises(ProviderError):
            provider._fetch_as_base64("https://example.com/f.txt")


def test_azure_email_fetch_oversized():
    provider = AzureEmailProvider()
    resp = MagicMock()
    resp.is_redirect = False
    resp.status_code = 200
    resp.headers = {"content-length": str(30 * 1024 * 1024)}
    dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=dns), \
         patch("httpx.stream", return_value=MagicMock(__enter__=MagicMock(return_value=resp))):
        with pytest.raises(ProviderError):
            provider._fetch_as_base64("https://example.com/big.bin")


def test_azure_email_fetch_no_host():
    provider = AzureEmailProvider()
    with pytest.raises(ProviderError):
        provider._fetch_as_base64("https:///nohost.bin")
