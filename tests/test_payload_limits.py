"""Boundary tests for configurable production payload limits (HTTP 413)."""
import base64
import uuid

import pytest


def _err_msg(resp):
    """Extract the error message from a 4xx response body (detail-wrapped)."""
    body = resp.json()
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        err = detail.get("error", detail)
        if isinstance(err, dict):
            return err.get("message", "")
    return str(detail)


def _send(client, channel, contact, message):
    return client.post(
        "/api/v1/notifications/send",
        json={"channels": [{"channel": channel, "contact": contact}], "message": message},
    )


def test_sms_message_limit_boundaries(client, monkeypatch):
    """SMS limit 1600: below and exactly -> 202, above -> 413."""
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    contact = "+919888777666"
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        assert _send(client, "sms", contact, "x" * 1599).status_code == 202
        assert _send(client, "sms", contact, "x" * 1600).status_code == 202
        r = _send(client, "sms", contact, "x" * 1601)
    assert r.status_code == 413
    body = r.json()
    detail = body.get("detail", body)
    err = detail.get("error", detail)
    assert "1600" in err.get("message", "") or "sms" in err.get("message", "").lower()


def test_whatsapp_message_limit_boundaries(client, monkeypatch):
    """WhatsApp limit 4096: below and exactly -> 202, above -> 413."""
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    contact = "+919888777666"
    with patch("app.providers.vonage_provider.VonageWhatsAppProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_whatsapp", "m2", "submitted")
        assert _send(client, "whatsapp", contact, "x" * 4095).status_code == 202
        assert _send(client, "whatsapp", contact, "x" * 4096).status_code == 202
        r = _send(client, "whatsapp", contact, "x" * 4097)
    assert r.status_code == 413
    assert "4096" in _err_msg(r)


def test_email_message_limit_boundaries(client, monkeypatch):
    """Email limit 100000: below and exactly -> 202, above -> 413."""
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.azure_provider.AzureEmailProvider.send") as fake:
        fake.return_value = ProviderResult("azure_email", "m3", "submitted")
        assert _send(client, "email", "user@example.com", "x" * 99999).status_code == 202
        assert _send(client, "email", "user@example.com", "x" * 100000).status_code == 202
        r = _send(client, "email", "user@example.com", "x" * 100001)
    assert r.status_code == 413
    assert "100000" in _err_msg(r)


def test_limits_are_configurable(client, monkeypatch):
    """Lowering a limit via env is respected (SMS capped at 10 for the test)."""
    from unittest.mock import patch

    from app.config import get_settings
    from app.providers.base import ProviderResult

    monkeypatch.setenv("SMS_MAX_MESSAGE_LENGTH", "10")
    get_settings.cache_clear()
    contact = "+919888777666"
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m4", "submitted")
        assert _send(client, "sms", contact, "x" * 10).status_code == 202
        r = _send(client, "sms", contact, "x" * 11)
    assert r.status_code == 413
    get_settings.cache_clear()


def test_legacy_send_payload_limit(client, monkeypatch):
    """Legacy /send also enforces the channel message limit (413)."""
    from app.config import get_settings

    monkeypatch.setenv("SMS_MAX_MESSAGE_LENGTH", "5")
    get_settings.cache_clear()
    r = client.post("/send", json={"channel": "sms", "contact": "9887270348",
                                   "message": "x" * 6})
    assert r.status_code == 413
    get_settings.cache_clear()


def test_total_request_size_limit(client, monkeypatch):
    """Total request size limit returns 413 when exceeded."""
    from unittest.mock import patch

    from app.config import get_settings
    from app.providers.base import ProviderResult

    monkeypatch.setenv("MAX_REQUEST_SIZE_BYTES", "100")  # tiny
    get_settings.cache_clear()
    contact = "+919888777666"
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m5", "submitted")
        # A normal message body (~90 bytes) exceeds the 100-byte total limit.
        r = _send(client, "sms", contact, "hello world, this is a test message")
    assert r.status_code == 413
    assert "request size" in _err_msg(r)
    get_settings.cache_clear()


def test_email_attachment_file_and_page_limits(client, monkeypatch):
    """Email attachments: oversized file (10MB) and pages (100) return 413."""
    from unittest.mock import patch

    from app.config import get_settings
    from app.providers.base import ProviderResult

    # Raise the message limit so the email reaches the attachment check.
    monkeypatch.setenv("EMAIL_MAX_MESSAGE_LENGTH", "1000000")
    # Raise the total request size so the file-size check is evaluated first.
    monkeypatch.setenv("MAX_REQUEST_SIZE_BYTES", "100000000")
    get_settings.cache_clear()

    big_file = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()  # > 10MB
    with patch("app.providers.azure_provider.AzureEmailProvider.send_delivery") as fake:
        fake.return_value = ProviderResult("azure_email", "m6", "submitted")
        r = client.post(
            "/api/v1/notifications/event",
            json={"event_type": "test", "deliveries": [{
                "channel": "email",
                "payload": {
                    "recipient": "user@example.com",
                    "message": "hi",
                    "attachments": [{"name": "big.bin", "content_base64": big_file}],
                },
            }]},
        )
    assert r.status_code == 413
    assert "attachment" in _err_msg(r)

    with patch("app.providers.azure_provider.AzureEmailProvider.send_delivery") as fake:
        fake.return_value = ProviderResult("azure_email", "m7", "submitted")
        r = client.post(
            "/api/v1/notifications/event",
            json={"event_type": "test", "deliveries": [{
                "channel": "email",
                "payload": {
                    "recipient": "user@example.com",
                    "message": "hi",
                        "attachments": [{
                            "name": "doc.pdf", "pages": 101,
                            "content_base64": base64.b64encode(b"pdf").decode(),
                        }],
                },
            }]},
        )
    assert r.status_code == 413
    assert "101 pages" in _err_msg(r)
    get_settings.cache_clear()


def test_message_limits_helper():
    """validate_message_limits / validate_request_size / validate_attachment_limits."""
    from app.schemas import Channel
    from app.validation import (
        validate_attachment_limits,
        validate_message_limits,
        validate_request_size,
    )

    assert validate_message_limits("hi", [Channel.sms]) is None
    assert "1600" in validate_message_limits("x" * 2000, [Channel.sms])
    assert validate_message_limits("x" * 2000, [Channel.email]) is None

    from pydantic import BaseModel

    class P(BaseModel):
        message: str = "hello"

    assert validate_request_size(P()) is None

    class Att:
        name = "a.bin"
        content_base64 = base64.b64encode(b"x" * 100).decode()
        pages = None

    class AttBig:
        name = "big.bin"
        content_base64 = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
        pages = None

    class AttPages:
        name = "d.pdf"
        content_base64 = None
        pages = 200

    assert validate_attachment_limits([Att()]) is None
    assert "attachment" in validate_attachment_limits([AttBig()])
    assert "pages" in validate_attachment_limits([AttPages()])
