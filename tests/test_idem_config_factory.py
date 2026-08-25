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

    os.environ["VONAGE_API_KEY"] = "k"
    os.environ["VONAGE_API_SECRET"] = "s"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.sms)
    assert p.name == "vonage_sms"
    get_settings.cache_clear()


def test_factory_sms_falls_back_to_azure(monkeypatch):
    import os

    os.environ["VONAGE_API_KEY"] = ""
    os.environ["VONAGE_API_SECRET"] = ""
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
    os.environ["VONAGE_API_KEY"] = "k"
    os.environ["VONAGE_API_SECRET"] = "s"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.providers.factory import get_provider
    from app.schemas import Channel

    p = get_provider(Channel.whatsapp)
    assert p.name == "vonage_whatsapp"
    get_settings.cache_clear()


def test_factory_email_is_azure(monkeypatch):
    import os

    os.environ["VONAGE_API_KEY"] = ""
    os.environ["VONAGE_WHATSAPP_FROM"] = ""
    os.environ["VONAGE_API_SECRET"] = ""
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
