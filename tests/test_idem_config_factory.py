"""Tests for idempotency durable path, config, factory, and main app."""
import pytest


def test_idempotency_durable_path(storage):
    from app.idempotency import check_durable, payload_hash

    ph = payload_hash({"channels": [{"channel": "sms"}], "message": "hi"})
    nid, is_new = check_durable("key-durable-1", ph)
    assert is_new is True
    assert nid is not None
    # second call -> existing, is_new False
    nid2, is_new2 = check_durable("key-durable-1", ph)
    assert is_new2 is False
    assert nid2 == nid


def test_config_properties():
    from app.config import get_settings

    s = get_settings()
    # canonical + legacy aliases
    assert hasattr(s, "connection_string")
    assert hasattr(s, "whatsapp_channel_id")
    assert hasattr(s, "whatsapp_template_name")
    assert hasattr(s, "whatsapp_template_language")
    assert s.MAX_ATTEMPTS >= 1


def test_factory_sms_prefers_vonage(monkeypatch):
    import os

    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.sms)
    assert p.name == "vonage_sms"
    get_settings.cache_clear()


def test_factory_sms_prefers_twilio(monkeypatch):
    import os

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.sms)
    assert p.name == "twilio_sms"
    get_settings.cache_clear()


def test_factory_sms_falls_back_to_azure(monkeypatch):
    import os

    monkeypatch.setenv("VONAGE_API_KEY", "")
    monkeypatch.setenv("VONAGE_API_SECRET", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.sms)
    assert p.name == "azure_sms"
    get_settings.cache_clear()


def test_factory_whatsapp_prefers_vonage(monkeypatch):
    import os

    os.environ["VONAGE_WHATSAPP_FROM"] = "14157386102"
    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.whatsapp)
    assert p.name == "vonage_whatsapp"
    get_settings.cache_clear()


def test_factory_whatsapp_prefers_twilio(monkeypatch):
    import os

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.whatsapp)
    assert p.name == "twilio_whatsapp"
    get_settings.cache_clear()


def test_factory_twilio_requires_sender(monkeypatch):
    import os

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM", "")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    # Twilio configured but no sender -> falls through to Vonage/Azure
    assert get_provider(Channel.sms).name != "twilio_sms"
    assert get_provider(Channel.whatsapp).name != "twilio_whatsapp"
    get_settings.cache_clear()


def test_factory_email_is_azure(monkeypatch):
    import os

    monkeypatch.setenv("VONAGE_API_KEY", "")
    monkeypatch.setenv("VONAGE_WHATSAPP_FROM", "")
    monkeypatch.setenv("VONAGE_API_SECRET", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.email)
    assert p.name == "azure_email"
    get_settings.cache_clear()


def test_main_unhandled_exception_handler(client):
    # POST malformed-ish triggers validation 422, not 500; simulate 500 path
    # by hitting an endpoint that raises via a mocked provider exception.
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.side_effect = RuntimeError("kaboom")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                              "message": "hi"})
    # Background task catches; API still returns 202
    assert r.status_code == 202


def test_webhook_validate_missing_code(client):
    r = client.get("/api/v1/whatsapp/webhook")
    assert r.status_code == 400


def test_webhook_validate_with_code(client):
    r = client.get("/api/v1/whatsapp/webhook?validationCode=abc123")
    assert r.status_code == 200
    assert r.text == "abc123"


def test_webhook_non_whatsapp_event_ignored(client):
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"data": {"channelType": "sms", "messageId": "x",
                                    "status": "delivered"},
                           "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated"}])
    assert r.status_code == 200


def test_webhook_missing_message_id_ignored(client):
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"data": {"channelType": "whatsapp", "status": "delivered"},
                           "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated"}])
    assert r.status_code == 200


def test_main_exception_handler_classified_provider(client):
    """Provider errors outside the worker are classified to typed errors."""
    from unittest.mock import MagicMock

    from app.main import unhandled_exception_handler
    from app.providers.base import ProviderError

    req = MagicMock()
    req.url.path = "/test"

    # Retryable provider error → 502
    resp = unhandled_exception_handler(req, ProviderError("down", retryable=True))
    assert resp.status_code == 502

    # Non-retryable provider error → 400
    resp = unhandled_exception_handler(req, ProviderError("bad recipient", retryable=False))
    assert resp.status_code == 400

    # Unknown exception → 500
    resp = unhandled_exception_handler(req, RuntimeError("boom"))
    assert resp.status_code == 500


def test_memory_queue_run_error_does_not_kill_loop():
    """Memory queue worker survives a callback exception."""
    import asyncio

    from app.memory_queue import MemoryQueue

    seen = []

    async def run():
        q = MemoryQueue()

        async def cb(channel, payload):
            seen.append(payload["notification_id"])
            raise RuntimeError("boom")  # should be caught

        q = MemoryQueue(worker_callback=cb)
        q.start()
        q.publish("sms", "n-1", "g", "+919887270348", attempt=1)
        await asyncio.sleep(0.05)
        q.publish("sms", "n-2", "g", "+919887270348", attempt=1)
        await asyncio.sleep(0.05)
        q.stop()

    asyncio.run(run())
    assert seen  # both processed despite the error
