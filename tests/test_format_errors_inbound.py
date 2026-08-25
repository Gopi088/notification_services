"""Tests for message formatting, error handling, inbound webhook, and SMS templates."""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.providers.base import ProviderError, ProviderResult


# ---------- message_format ----------

def test_format_sms_plain():
    from app.message_format import format_sms

    assert format_sms("hello") == "hello"


def test_format_sms_template(tmp_path, monkeypatch):
    from app.message_format import format_sms

    sms_dir = tmp_path / "sms"
    sms_dir.mkdir(parents=True)
    (sms_dir / "otp.txt").write_text("Your OTP is {{body}}", encoding="utf-8")
    monkeypatch.setattr("app.templates._templates_dir", lambda: tmp_path)
    result = format_sms("482913", template_name="otp")
    assert result == "Your OTP is 482913"


def test_format_sms_template_with_params(tmp_path, monkeypatch):
    from app.message_format import format_sms

    sms_dir = tmp_path / "sms"
    sms_dir.mkdir(parents=True)
    (sms_dir / "greeting.txt").write_text("Hi {{name}}, your {{body}}", encoding="utf-8")
    monkeypatch.setattr("app.templates._templates_dir", lambda: tmp_path)
    result = format_sms("order confirmed", template_name="greeting", template_params={"name": "Rahul"})
    assert result == "Hi Rahul, your order confirmed"


def test_format_sms_template_missing_fallback():
    from app.message_format import format_sms

    result = format_sms("hello", template_name="nonexistent-template")
    assert result == "hello"


def test_format_whatsapp_text():
    from app.message_format import format_whatsapp

    r = format_whatsapp("hello")
    assert r == {"text": "hello"}


def test_format_whatsapp_template():
    from app.message_format import format_whatsapp

    r = format_whatsapp("hi", template_name="test_template", template_language="en",
                        template_params={"name": "Rahul"})
    assert r["template"] == "test_template"
    assert r["params"]["name"] == "Rahul"


def test_format_email_plain():
    from app.message_format import format_email

    r = format_email("hello", subject="Test")
    assert "hello" in r["html"]
    assert r["subject"] == "Test"


def test_format_email_template(tmp_path, monkeypatch):
    from app.message_format import format_email

    email_dir = tmp_path / "email"
    email_dir.mkdir(parents=True)
    (email_dir / "welcome.html").write_text("<h1>{{subject}}</h1><p>{{body}}</p>", encoding="utf-8")
    monkeypatch.setattr("app.templates._templates_dir", lambda: tmp_path)
    r = format_email("Welcome to the service", template_name="welcome",
                     template_params={"subject": "Hello!"})
    assert "Hello!" in r["html"]
    assert r["subject"] == "Hello!"


def test_format_for_channel_sms():
    from app.message_format import format_for_channel

    r = format_for_channel("sms", "hello")
    assert r == "hello"


def test_format_for_channel_whatsapp():
    from app.message_format import format_for_channel

    r = format_for_channel("whatsapp", "hello")
    assert r == {"text": "hello"}


def test_format_for_channel_email():
    from app.message_format import format_for_channel

    r = format_for_channel("email", "hello")
    assert "hello" in r["html"]


def test_format_for_channel_unknown():
    from app.message_format import MessageFormatError, format_for_channel

    with pytest.raises(MessageFormatError):
        format_for_channel("telegram", "hi")


def test_render_template_text():
    from app.message_format import render_template_text

    r = render_template_text("Hello {{name}}, your {{body}}", {"name": "Rahul", "body": "code"})
    assert r == "Hello Rahul, your code"
    # missing placeholder -> empty
    assert render_template_text("{{x}}", {}) == ""


# ---------- errors ----------

def test_app_error_to_dict():
    from app.errors import AppError, ValidationError, NotFoundError, RateLimitError

    err = AppError("test")
    d = err.to_dict()
    assert d["code"] == "internal_error"
    assert d["message"] == "test"

    v = ValidationError("bad input", field="contact")
    d = v.to_dict()
    assert d["code"] == "validation_error"
    assert d["field"] == "contact"

    assert NotFoundError().status_code == 404
    assert RateLimitError().status_code == 429
    assert RateLimitError().code == "rate_limited"


def test_classify_provider_error():
    from app.errors import classify_provider_error, ConfigurationError, ProviderUnavailableError, ValidationError

    r = classify_provider_error(ProviderError("timeout", retryable=True, error_code="500"))
    assert isinstance(r, ProviderUnavailableError)

    r = classify_provider_error(ProviderError("bad", retryable=False, error_code="400"))
    assert isinstance(r, ValidationError)

    r = classify_provider_error(ValueError("x"))
    assert r.code == "internal_error"


# ---------- inbound webhook ----------

def test_inbound_get(client):
    r = client.get("/api/v1/inbound")
    assert r.status_code == 200


def test_inbound_post_normalized(client, storage):
    r = client.post("/api/v1/inbound",
                    json={"channel": "sms", "from": "+919887270348", "to": "+1484",
                          "text": "Hello, I received the message",
                          "message_uuid": "inbound-msg-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True


def test_inbound_post_azure_format(client, storage):
    r = client.post("/api/v1/inbound",
                    json={"channelType": "whatsapp",
                          "from": "+919887270348",
                          "to": "+1484",
                          "message": "Thanks!",
                          "messageId": "az-in-1"})
    assert r.status_code == 200
    assert r.json()["accepted"] is True


def test_inbound_stored(client, storage):
    from app.storage import get_storage

    client.post("/api/v1/inbound",
                json={"channel": "sms", "from": "+919887270348", "to": "+1484",
                      "text": "stored message", "message_uuid": "store-1"})
    msgs = get_storage().list_inbound(limit=5)
    texts = [m["text"] for m in msgs if m["text"] == "stored message"]
    assert texts


def test_inbound_audit_recorded(client, storage):
    from app.audit import list_audit

    client.post("/api/v1/inbound",
                json={"channel": "sms", "from": "+919887270348", "to": "+1484",
                      "text": "audit check", "message_uuid": "aud-in-1"})

    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_received" in actions


def test_inbound_empty_text(client):
    r = client.post("/api/v1/inbound", json={"channel": "sms", "from": "+1", "text": ""})
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_inbound_auto_reply(client, monkeypatch, storage):
    import os

    os.environ["INBOUND_AUTO_REPLY"] = "true"
    os.environ["INBOUND_AUTO_REPLY_TEXT"] = "Thanks for your message!"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.vonage_provider import VonageSMSProvider

    with patch.object(VonageSMSProvider, "send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "auto-reply-id", "submitted")
        r = client.post("/api/v1/inbound",
                        json={"channel": "sms", "from": "+919887270348", "to": "+1484",
                              "text": "auto-reply test", "message_uuid": "auto-1"})
    assert r.status_code == 200
    assert r.json()["auto_reply"] == "Thanks for your message!"
    os.environ["INBOUND_AUTO_REPLY"] = "false"
    get_settings.cache_clear()


# ---------- SMS template via provider ----------

def test_vonage_sms_send_with_template_render(monkeypatch):
    import os
    import tempfile

    os.environ["MOCK_MODE"] = "false"
    os.environ["VONAGE_API_KEY"] = "k"
    os.environ["VONAGE_API_SECRET"] = "s"
    os.environ["VONAGE_SMS_FROM"] = "Vonage APIs"
    os.environ["AZURE_DEFAULT_COUNTRY_CODE"] = "91"
    from app.config import get_settings

    get_settings.cache_clear()

    from app.providers.vonage_provider import VonageSMSProvider

    provider = VonageSMSProvider()
    resp = MagicMock()
    resp.message_uuid = "sms-tpl-id"
    captured = {}

    class FakeMessages:
        def send(self, sms):
            captured["text"] = sms.text
            return resp

    with patch("vonage.Vonage") as mv:
        mv.return_value.messages = FakeMessages()
        # Create a real SMS template file
        tmpdir = tempfile.mkdtemp()
        sms_dir = os.path.join(tmpdir, "sms")
        os.makedirs(sms_dir, exist_ok=True)
        with open(os.path.join(sms_dir, "otp.txt"), "w") as f:
            f.write("Your OTP is {{body}}")
        import app.templates as tpl_mod
        from pathlib import Path

        monkeypatch.setattr(tpl_mod, "_templates_dir", lambda: Path(tmpdir))
        result = provider.send_with_template("9887270348", "482913", "otp")
        assert result.provider_message_id == "sms-tpl-id"
        assert captured["text"] == "Your OTP is 482913"
    get_settings.cache_clear()


# ---------- main exception handler ----------

def test_main_exception_handler_404(client):
    from app.errors import NotFoundError

    from app.main import unhandled_exception_handler
    from unittest.mock import MagicMock

    req = MagicMock()
    req.url.path = "/test"
    resp = unhandled_exception_handler(req, NotFoundError("not found"))
    assert resp.status_code == 404
    assert resp.body

def test_format_email_missing_template_raises(monkeypatch):
    """format_email raises MessageFormatError when the named template is missing."""
    import tempfile
    from pathlib import Path

    from app.message_format import MessageFormatError, format_email

    tmpdir = tempfile.mkdtemp()
    import app.templates as tpl_mod

    monkeypatch.setattr(tpl_mod, "_templates_dir", lambda: Path(tmpdir))
    with pytest.raises(MessageFormatError):
        format_email("hi", template_name="missing-template-name")


def test_main_exception_handler_provider_error(client):
    """Provider errors outside the worker are mapped to typed responses."""
    from unittest.mock import MagicMock

    from app.main import unhandled_exception_handler
    from app.providers.base import ProviderError

    req = MagicMock()
    req.url.path = "/test"
    resp = unhandled_exception_handler(req, ProviderError("bad provider", retryable=True))
    assert resp.status_code == 502


def test_main_exception_handler_config_error(client):
    from unittest.mock import MagicMock

    from app.main import unhandled_exception_handler
    from app.providers.base import ProviderConfigError

    req = MagicMock()
    req.url.path = "/test"
    resp = unhandled_exception_handler(req, ProviderConfigError("no key"))
    assert resp.status_code == 500


def test_inbound_azure_nested_data(client, storage):
    """Azure-style inbound nested under data is normalized."""
    r = client.post("/api/v1/inbound",
                    json={"data": {"from": "+919887270348", "to": "+1484",
                                   "message": "nested reply", "messageId": "nest-1"}})
    assert r.status_code == 200
    assert r.json()["accepted"] is True


def test_errors_context_and_field():
    from app.errors import ForbiddenError

    err = ForbiddenError("no access", field="channels")
    d = err.to_dict()
    assert d["field"] == "channels"
    assert err.status_code == 403
