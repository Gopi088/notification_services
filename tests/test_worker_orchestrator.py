"""Additional worker + orchestrator path coverage."""
from unittest.mock import patch

import pytest

from app.providers.base import ProviderResult
from app.storage import QUEUED, SUBMITTED, get_storage


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r


def test_worker_unexpected_exception(storage, fake_redis_client):
    from app.worker import process_message

    nid = storage.create_notification(
        message_id="unexp-1", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.side_effect = RuntimeError("boom")
        ok = process_message("sms", {"notification_id": nid, "channel": "sms",
                                     "recipient": "+919887270348", "attempt": 1})
    assert ok is True
    row = storage.get_notification(nid)
    # Unexpected provider failures must be recoverable, not left processing
    # after their Redis stream entry has been acknowledged.
    assert row["status"] == "retrying"
    from app import queue as q
    assert fake_redis_client.xlen(q.retry_stream_name()) == 1


def test_process_retry_stream_moves_due(fake_redis_client, storage):
    import time

    from app import queue as q
    from app.worker import process_retry_stream

    nid = storage.create_notification(
        message_id="retry-1", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    # publish a retry that is already due (scheduled in the past)
    q.publish_retry("sms", nid, "g1", "+919887270348", 2, scheduled_at=time.time() - 100)
    requeued = process_retry_stream()
    assert requeued == 1
    # now on the channel stream
    assert fake_redis_client.xlen(q.stream_name("sms")) == 1
    # retry stream drained
    assert fake_redis_client.xlen(q.retry_stream_name()) == 0


def test_worker_delivery_message_helpers():
    from app.orchestrator import _delivery_message
    from app.schemas import Channel

    assert _delivery_message(Channel.sms, {"message": "hello"}) == "hello"
    assert _delivery_message(Channel.sms, {}, "fallback data") == "fallback data"
    assert _delivery_message(Channel.whatsapp, {"template": {"id": "tpl1"}}, None) == "[tpl1]"
    assert _delivery_message(Channel.email, {"html": "<p>x</p>"}, None) == "<p>x</p>"
    assert _delivery_message(Channel.sms, {}, None) == ""


def test_orchestrate_event_queue_disabled(client):
    """With QUEUE_ENABLED=false, event deliveries dispatch in-process."""
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post(
            "/api/v1/notifications/event",
            json={"event_type": "test",
                  "deliveries": [{"channel": "sms",
                                  "payload": {"recipient": "+919887270348",
                                              "message": "hi"}}]},
        )
    assert r.status_code == 202


def test_delivery_detail_elapsed():
    from datetime import datetime, timedelta, timezone

    from app.orchestrator import _delivery_detail

    old = {"created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
           "status": "queued"}
    detail = _delivery_detail(old)
    assert detail["timed_out"] is True
    assert detail["elapsed_seconds"] > 100

    fresh = {"created_at": datetime.now(timezone.utc).isoformat(), "status": "queued"}
    d2 = _delivery_detail(fresh)
    assert d2["timed_out"] is False


def test_get_message_summary_missing(storage):
    from app.orchestrator import get_message_summary

    assert get_message_summary("missing-id") is None


def test_get_group_summary_missing(storage):
    from app.orchestrator import get_group_summary

    assert get_group_summary("missing-group") is None


def test_memory_queue_publish_and_length(monkeypatch):
    """QUEUE_BACKEND=memory enqueues jobs without Redis."""
    import asyncio

    from app.config import get_settings

    get_settings.cache_clear()
    from app.memory_queue import MemoryQueue, get_memory_queue, reset_memory_queue

    reset_memory_queue()
    q = get_memory_queue()
    q.publish("sms", "n-1", "g-1", "+919887270348", attempt=1)
    q.publish("sms", "n-2", "g-1", "+919887270348", attempt=1)
    assert q.queue_length("sms") == 2
    q.publish("whatsapp", "n-3", "g-1", "+919887270348", attempt=1)
    assert q.queue_length("whatsapp") == 1
    reset_memory_queue()
    get_settings.cache_clear()


def test_memory_queue_worker_callback(monkeypatch):
    """MemoryQueue runs the worker callback for each job."""
    import asyncio

    from app.memory_queue import MemoryQueue

    seen = []

    async def run():
        q = MemoryQueue()

        async def cb(channel, payload):
            seen.append((channel, payload["notification_id"]))
            q.stop()

        q = MemoryQueue(worker_callback=cb)
        q.start()
        q.publish("sms", "n-1", "g", "+919887270348", attempt=1)
        await asyncio.sleep(0.1)
        q.stop()

    asyncio.run(run())
    assert ("sms", "n-1") in seen


def test_group_summary_read_and_acknowledged_fields(client, storage):
    """Status response includes read_at / acknowledged_at fields."""
    import uuid

    from app.orchestrator import get_group_summary

    gid = str(uuid.uuid4())
    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="hi", status="delivered", group_id=gid,
    )
    storage.mark_read(nid)
    storage.mark_acknowledged(nid, ack_type="reply", ack_message="YES", ack_source="inbound_whatsapp")
    summary = get_group_summary(gid)
    ch = summary["channels"][0]
    assert ch["read_at"] is not None
    assert ch["acknowledged_at"] is not None
    assert ch["acknowledgement_type"] == "reply"


def test_memory_queue_stop_idempotent():
    from app.memory_queue import MemoryQueue

    q = MemoryQueue()
    q.stop()
    q.stop()  # no error


def test_memory_queue_publish_with_extra_fields():
    from app.memory_queue import MemoryQueue

    q = MemoryQueue()
    q.publish("sms", "n-x", "g", "+919887270348", attempt=1, idempotency_key="k1", request_id="r1")
    assert q.queue_length("sms") == 1
    q.stop()
