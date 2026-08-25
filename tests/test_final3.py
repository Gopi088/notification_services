"""Final coverage push to >=90%."""
import json
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def vonage_env(monkeypatch):
    os.environ["MOCK_MODE"] = "false"
    os.environ["VONAGE_API_KEY"] = "k"
    os.environ["VONAGE_API_SECRET"] = "s"
    os.environ["VONAGE_SMS_FROM"] = "Vonage APIs"
    os.environ["VONAGE_WHATSAPP_FROM"] = "14157386102"
    os.environ["AZURE_DEFAULT_COUNTRY_CODE"] = "91"
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

from app.providers.base import ProviderResult


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r


# ---------- idempotency redis error paths ----------

def test_idempotency_redis_errors(monkeypatch):
    from app.idempotency import check_redis, store_redis

    def boom():
        raise RuntimeError("redis down")

    import app.idempotency as idem

    monkeypatch.setattr(idem, "_redis", boom)
    assert check_redis("k") is None  # fails open
    store_redis("k", "n")  # fails silently
    # normalize edge cases
    from app.idempotency import normalize_client_key, payload_hash

    assert normalize_client_key("  x  ") == "x"
    assert len(payload_hash({"a": 1})) == 64


# ---------- ratelimit remaining branches ----------

def test_ratelimit_check_recipient_channel(fake_redis_client, monkeypatch):
    import os

    os.environ["RATELIMIT_ENABLED"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    from app import ratelimit

    r = ratelimit.check_recipient("+919887270348")
    assert r.allowed
    r2 = ratelimit.check_channel("whatsapp")
    assert r2.allowed
    r3 = ratelimit.check_provider("vonage_sms", 10)
    assert r3.allowed
    os.environ["RATELIMIT_ENABLED"] = "false"
    get_settings.cache_clear()


# ---------- queue remaining helpers ----------

def test_queue_publish_retry_and_dlq_with_error(fake_redis_client):
    from app import queue as q

    q.publish_retry("whatsapp", "n1", "g", "+919887270348", 2, 100.0)
    q.publish_dlq("whatsapp", "n2", "g", "+919887270348", 1, reason="r", error_code="e", error_message="m")
    assert fake_redis_client.xlen(q.retry_stream_name()) == 1
    assert fake_redis_client.xlen(q.dlq_stream_name()) == 1


# ---------- base provider fallbacks ----------

def test_base_provider_send_with_template_fallback():
    from app.providers.base import NotificationProvider

    class P(NotificationProvider):
        name = "p"

        def send(self, contact, message):
            return ProviderResult("p", "id", "submitted")

    p = P()
    r = p.send_with_template("+1", "hi", "tpl")
    assert r.provider_message_id == "id"
    r2 = p.send_delivery({"recipient": "+1", "message": "hi"})
    assert r2.provider_message_id == "id"


# ---------- logging_config remaining ----------

def test_logging_redact_pii():
    from app.logging_config import _redact_pii, mask

    assert "0348" not in _redact_pii("call +919887270348 now")
    assert mask("short") == "short"


# ---------- migrate status/unknown ----------

def test_migrate_status_and_unknown(monkeypatch, capsys, tmp_path):
    import os

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "DATABASE_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr("sys.argv", ["migrate", "status"])
    from app import migrate

    assert migrate.main() == 0
    monkeypatch.setattr("sys.argv", ["migrate", "down"])
    assert migrate.main() == 0


# ---------- vonage sms/whatsapp remaining branches ----------

def test_vonage_whatsapp_send_delivery_payload(vonage_env):
    from app.providers.vonage_provider import VonageWhatsAppProvider

    provider = VonageWhatsAppProvider()
    with patch("app.providers.vonage_provider.requests.post") as mp:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message_uuid": "vu-1"}
        mp.return_value = resp
        r = provider.send_delivery({"recipient": "+919887270348", "message": "hi"})
    assert r.provider_message_id == "vu-1"


def test_vonage_sms_send_delivery(vonage_env):
    from app.providers.vonage_provider import VonageSMSProvider

    provider = VonageSMSProvider()
    resp = MagicMock()
    resp.message_uuid = "su-1"
    with patch("vonage.Vonage") as mv:
        mv.return_value.messages.send.return_value = resp
        r = provider.send_delivery({"recipient": "+919887270348", "message": "hi"})
    assert r.provider_message_id == "su-1"


# ---------- legacy notifications router status ----------

def test_legacy_status_flow(client, storage):
    import uuid

    mid = str(uuid.uuid4())
    storage.create_notification(message_id=mid, channel="sms", recipient="+919887270348",
                                message="hi", status="queued")
    r = client.get(f"/status/{mid}")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_legacy_status_not_found(client):
    r = client.get("/status/missing-id")
    assert r.status_code == 404
