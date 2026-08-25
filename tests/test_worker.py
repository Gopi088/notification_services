"""Tests for the worker: consume, deliver, retry, DLQ, idempotency."""
import json
import time
from unittest.mock import patch

import fakeredis
import pytest

from app.providers.base import ProviderError, ProviderResult
from app.storage import (
    DEAD_LETTERED,
    DELIVERED,
    FAILED,
    QUEUED,
    RETRYING,
    SUBMITTED,
    get_storage,
)


@pytest.fixture()
def fake_redis_client(monkeypatch):
    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)

    def _client():
        return r

    import app.queue as q

    monkeypatch.setattr(q, "_client", _client)
    monkeypatch.setattr(q, "_client", _client)
    from app import idempotency as idem

    monkeypatch.setattr(idem, "_redis", _client)
    return r


def test_process_message_success(storage, fake_redis_client):
    from app import queue as q
    from app.worker import process_message

    nid = storage.create_notification(
        message_id="w1", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_send:
        fake_send.return_value = ProviderResult("vonage_sms", "uuid-1", "submitted")
        ok = process_message("sms", {
            "notification_id": nid, "channel": "sms",
            "recipient": "+919887270348", "attempt": 1,
        })
    assert ok is True
    row = storage.get_notification(nid)
    assert row["status"] == SUBMITTED
    assert row["provider_message_id"] == "uuid-1"
    # attempts recorded
    assert len(storage.list_attempts(nid)) == 1


def test_process_message_retryable_routes_to_retry(storage, fake_redis_client):
    from app.worker import process_message

    nid = storage.create_notification(
        message_id="w2", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_send:
        fake_send.side_effect = ProviderError("timeout", retryable=True, error_code="TIMEOUT")
        ok = process_message("sms", {
            "notification_id": nid, "channel": "sms",
            "recipient": "+919887270348", "attempt": 1,
        })
    assert ok is True
    row = storage.get_notification(nid)
    assert row["status"] == RETRYING
    assert row["retry_count"] == 1
    # retry stream has an entry
    from app import queue as q

    assert fake_redis_client.xlen(q.retry_stream_name()) == 1


def test_process_message_non_retryable_dlq(storage, fake_redis_client):
    from app import queue as q
    from app.worker import process_message

    nid = storage.create_notification(
        message_id="w3", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_send:
        fake_send.side_effect = ProviderError("bad recipient", retryable=False, error_code="400")
        ok = process_message("sms", {
            "notification_id": nid, "channel": "sms",
            "recipient": "+919887270348", "attempt": 1,
        })
    assert ok is True
    row = storage.get_notification(nid)
    assert row["status"] == FAILED
    assert fake_redis_client.xlen(q.dlq_stream_name()) == 1


def test_process_message_duplicate_skips_send(storage, fake_redis_client):
    from app.worker import process_message

    nid = storage.create_notification(
        message_id="w4", channel="sms", recipient="+919887270348",
        message="hi", status=SUBMITTED,  # already submitted (redelivery)
    )
    storage.set_provider_info(nid, "vonage_sms", "existing-id")
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_send:
        ok = process_message("sms", {
            "notification_id": nid, "channel": "sms",
            "recipient": "+919887270348", "attempt": 2,
        })
    assert ok is True
    fake_send.assert_not_called()


def test_process_message_missing_notification_dlq(storage, fake_redis_client):
    from app import queue as q
    from app.worker import process_message

    ok = process_message("sms", {"notification_id": "does-not-exist", "channel": "sms",
                                 "recipient": "+1", "attempt": 1})
    assert ok is True
    assert fake_redis_client.xlen(q.dlq_stream_name()) == 1


def test_run_once_consumes_and_acks(storage, fake_redis_client):
    from app import queue as q
    from app.worker import _run_once

    nid = storage.create_notification(
        message_id="w5", channel="sms", recipient="+919887270348",
        message="hi", status=QUEUED,
    )
    q.publish("sms", nid, "g", "+919887270348", attempt=1)
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_send:
        fake_send.return_value = ProviderResult("vonage_sms", "m-uuid", "submitted")
        worked = _run_once("sms", "w1")
    assert worked is True
    # message was acked (pending list empty)
    pending = fake_redis_client.xpending(q.stream_name("sms"), q.get_settings().QUEUE_CONSUMER_GROUP)
    assert pending is None or pending.get("pending", 0) == 0
