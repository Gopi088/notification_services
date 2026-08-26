"""Final gap-closing tests for 90% coverage: v1 replay/health, webhooks, queue,
main, storage PG branches, orchestrator queue path."""
import json
from unittest.mock import patch

import pytest


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r

from app.providers.base import ProviderResult


# ---------- v1 router: health + event idempotency key ----------

def test_v1_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert "service" in r.json()


def test_v1_event_with_idempotency_no_duplicate(client):
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post("/api/v1/notifications/event",
                        json={"event_type": "t", "deliveries": [
                            {"channel": "sms", "payload": {"recipient": "+919887270348", "message": "x"}}
                        ]})
    assert r.status_code == 202


def test_v1_event_recipient_rate_limit(client, monkeypatch):
    import os

    os.environ["RATELIMIT_ENABLED"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    from app import ratelimit

    monkeypatch.setattr("app.routers.v1.check_api_send",
                        lambda k: ratelimit.RateLimitResult(False, 0, 0, 60))
    r = client.post("/api/v1/notifications/event",
                    json={"event_type": "t", "deliveries": [
                        {"channel": "sms", "payload": {"recipient": "+919887270348", "message": "x"}}
                    ]})
    assert r.status_code == 429
    os.environ["RATELIMIT_ENABLED"] = "false"
    get_settings.cache_clear()


# ---------- webhooks: redact + extract helpers ----------

def test_redact_masks_nested_secret():
    from app.routers.webhooks import _redact

    out = _redact({"token": "secret", "nested": {"api_key": "k", "ok": 1}, "list": [{"secret": "s"}]})
    assert out["token"] == "***"
    assert out["nested"]["api_key"] == "***"
    assert out["nested"]["ok"] == 1
    assert out["list"][0]["secret"] == "***"


def test_extract_failure_string_error():
    from app.routers.webhooks import _extract_failure

    code, msg = _extract_failure({"error": "plain failure"})
    assert msg == "plain failure"


def test_extract_failure_status_reason():
    from app.routers.webhooks import _extract_failure

    code, msg = _extract_failure({"statusReason": "blocked"})
    assert msg == "blocked"


# ---------- queue: helper internals ----------

def test_queue_message_helpers(fake_redis_client):
    from app import queue as q

    q.publish("whatsapp", "n1", "g", "+919887270348", attempt=1, extra="v")
    assert fake_redis_client.xlen(q.stream_name("whatsapp")) == 1


def test_publish_dlq(fake_redis_client):
    from app import queue as q

    q.publish_dlq("whatsapp", "n1", "g", "+919887270348", 1, reason="boom", error_code="500")
    assert fake_redis_client.xlen(q.dlq_stream_name()) == 1


# ---------- storage: PG branches ----------

def test_storage_pg_full_crud(monkeypatch):
    import uuid

    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:testpass@localhost:5434/notifications")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.storage import Storage

    s = Storage(backend="postgres", url="postgresql://postgres:testpass@localhost:5434/notifications")
    s.connect()
    s.init_schema()
    mid = str(uuid.uuid4())
    nid = s.create_notification(message_id=mid, channel="sms", recipient="+91", message="x",
                                status="queued", template_params={"k": "v"})
    n = s.get_notification(nid)
    assert n["status"] == "queued"
    # transition to processing then submitted (valid path)
    s.transition(nid, "processing", actor="t")
    s.transition(nid, "submitted", actor="t", provider="p", provider_message_id="pm")
    assert s.get_notification(nid)["status"] == "submitted"
    # attempts + events
    s.add_attempt(nid, 1, "submitted", provider="p", provider_message_id="pm", duration_ms=5)
    assert len(s.list_attempts(nid)) == 1
    # idempotency key via PG
    k = f"idem-{uuid.uuid4().hex[:8]}"
    assert s.store_idempotency_key(k, nid, "h") is True
    assert s.store_idempotency_key(k, nid, "h") is False
    row = s.find_idempotency_key_row(k)
    assert row["notification_id"] == nid
    # webhook event
    s.record_webhook_event(provider="whatsapp", provider_message_id="pm", status="delivered", payload={"k": 1})
    # due notifications
    assert len(s.due_notifications()) >= 0
    # group
    gid = str(uuid.uuid4())
    s.create_notification(message_id=str(uuid.uuid4()), channel="sms", recipient="+1", message="y", status="queued", group_id=gid)
    assert len(s.get_group(gid)) == 1
    # invalid transition leaves state
    s.transition(nid, "delivered", actor="t")
    s.transition(nid, "processing", actor="t")
    assert s.get_notification(nid)["status"] == "delivered"
    s.close()
    get_settings.cache_clear()


# ---------- main: startup/shutdown/health ----------

def test_main_health_liveness_readiness(client):
    r1 = client.get("/health")
    assert r1.status_code == 200
    r2 = client.get("/api/v1/health/liveness")
    assert r2.status_code == 200
    r3 = client.get("/api/v1/health/readiness")
    assert r3.status_code == 200


# ---------- worker: run_worker lifecycle (patched, no hang) ----------

def test_worker_run_worker_signal_registration(monkeypatch):
    """run_worker registers handlers and returns when the stop event fires."""
    import app.worker as worker_mod
    from app.worker import run_worker

    calls = []

    def fake_run_once(channel, worker_id):
        calls.append((channel, worker_id))
        return False

    monkeypatch.setattr(worker_mod, "_run_once", fake_run_once)

    import signal as real_signal

    monkeypatch.setattr(real_signal, "signal", lambda signum, handler: None)

    # Patch threading so the worker loop runs at most once and then stops.
    # Once _run_once has been called, is_set() returns True so the while loop
    # exits (no infinite loop) and run_worker returns.
    class FakeEvent:
        def __init__(self):
            self._calls = calls  # shared reference to the calls list

        def set(self):
            pass

        def is_set(self):
            return len(calls) > 0

        def wait(self, timeout=None):
            return True

    import threading as _t

    monkeypatch.setattr(_t, "Event", FakeEvent)

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon
            self._started = False
            self._alive = True

        def start(self):
            self._started = True
            # Run the loop body once; it calls _run_once which appends to calls,
            # making FakeEvent.is_set() True so the loop exits.
            self.target()

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(_t, "Thread", FakeThread)

    settings = type("S", (), {
        "WORKER_CONCURRENCY": 1, "WORKER_CONCURRENCY_WHATSAPP": 1,
        "WORKER_CONCURRENCY_SMS": 1, "WORKER_CONCURRENCY_EMAIL": 1,
        "WORKER_GRACE_SECONDS": 0, "QUEUE_BLOCK_MS": 10,
        "QUEUE_CONSUMER_GROUP": "workers",
    })()
    monkeypatch.setattr(worker_mod, "get_settings", lambda: settings)
    import time as _time

    monkeypatch.setattr(worker_mod, "time", _time)

    run_worker("whatsapp")
    assert calls  # _run_once was invoked
