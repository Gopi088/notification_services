"""Prove MOCK_MODE=true prevents ANY real provider call (Twilio + Azure)."""
import uuid
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MOCK_MODE", "true")
    # Provide enough config so providers reach the MOCK short-circuit (they
    # return before validating credentials, but keep env realistic anyway).
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+17372508034")
    monkeypatch.setenv("TWILIO_WHATSAPP_CONTENT_SID", "HXtest")
    monkeypatch.setenv("AZURE_DEFAULT_COUNTRY_CODE", "91")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_twilio_sms_mock_does_not_call_requests():
    from app.providers.twilio_provider import TwilioSMSProvider

    with patch("app.providers.twilio_provider.requests.post") as fake_post, \
         patch("app.providers.twilio_provider.requests.get") as fake_get:
        result = TwilioSMSProvider().send("9887270348", "hi")
    fake_post.assert_not_called()
    fake_get.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_twilio_whatsapp_mock_does_not_call_requests():
    from app.providers.twilio_provider import TwilioWhatsAppProvider

    with patch("app.providers.twilio_provider.requests.post") as fake_post, \
         patch("app.providers.twilio_provider.requests.get") as fake_get:
        result = TwilioWhatsAppProvider().send("9887270348", "hi")
    fake_post.assert_not_called()
    fake_get.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_twilio_whatsapp_template_mock_does_not_call_requests():
    from app.providers.twilio_provider import TwilioWhatsAppProvider

    with patch("app.providers.twilio_provider.requests.post") as fake_post:
        result = TwilioWhatsAppProvider().send_with_template(
            "9887270348", "ignored", "test_template"
        )
    fake_post.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_azure_sms_mock_does_not_call_sdk():
    from app.providers.azure_provider import AzureSMSProvider

    with patch("azure.communication.sms.SmsClient.from_connection_string") as factory:
        result = AzureSMSProvider().send("9887270348", "hi")
    factory.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_azure_email_mock_does_not_call_sdk():
    from app.providers.azure_provider import AzureEmailProvider

    with patch("azure.communication.email.EmailClient.from_connection_string") as factory:
        result = AzureEmailProvider().send_delivery(
            {"recipient": "a@b.com", "message": "x", "subject": "s"}
        )
    factory.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_azure_whatsapp_mock_does_not_call_sdk():
    from app.providers.azure_provider import AzureWhatsAppProvider

    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string") as factory:
        result = AzureWhatsAppProvider().send("9887270348", "hi")
    factory.assert_not_called()
    assert result.provider_message_id.startswith("mock-")


def test_mock_mode_blocks_all_channels_via_api(client):
    """End-to-end: sending via the API in mock mode never hits a real provider."""
    import json

    from app.providers.base import ProviderResult

    # Patch every provider send path; in MOCK_MODE none should be invoked.
    patches = [
        patch("app.providers.vonage_provider.VonageSMSProvider.send",
              side_effect=AssertionError("vonage sms must not run in mock mode")),
        patch("app.providers.vonage_provider.VonageWhatsAppProvider.send",
              side_effect=AssertionError("vonage wa must not run in mock mode")),
        patch("app.providers.azure_provider.AzureEmailProvider.send",
              side_effect=AssertionError("azure email must not run in mock mode")),
    ]
    with __import__("contextlib").ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [
                            {"channel": "sms", "contact": "+919887270348"},
                            {"channel": "whatsapp", "contact": "+919887270348"},
                            {"channel": "email", "contact": "a@b.com"},
                        ], "message": "load safe"})
    assert r.status_code == 202
    for ch in r.json()["channels"]:
        assert ch["status"] == "queued"
