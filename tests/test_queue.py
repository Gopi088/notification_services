"""Tests for the queue module (Redis Streams) using fakeredis."""
import pytest


@pytest.fixture()
def fake_redis(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r


def test_publish_creates_stream(fake_redis):
    from app import queue as q

    eid = q.publish("sms", "n-1", "g-1", "+919887270348", attempt=1)
    assert eid
    length = fake_redis.xlen(q.stream_name("sms"))
    assert length == 1


def test_publish_retry_and_dlq(fake_redis):
    from app import queue as q

    q.publish_retry("sms", "n-2", "g-1", "+919887270348", 2, 1000000.0)
    assert fake_redis.xlen(q.retry_stream_name()) == 1
    q.publish_dlq("sms", "n-3", "g-1", "+919887270348", 1, reason="boom")
    assert fake_redis.xlen(q.dlq_stream_name()) == 1


def test_consume_and_ack(fake_redis):
    from app import queue as q

    q.publish("sms", "n-4", "g-1", "+919887270348", attempt=1)
    q.ensure_group("sms")
    entries = q.consume("sms", "worker-1", block_ms=10)
    assert entries
    # find the entry id
    for _stream, messages in entries:
        for eid, fields in messages:
            q.ack("sms", eid)
    # after ack, nothing pending
    pending = fake_redis.xpending(q.stream_name("sms"), q.get_settings().QUEUE_CONSUMER_GROUP)
    assert pending is None or pending.get("pending", 0) == 0


def test_ensure_group_idempotent(fake_redis):
    from app import queue as q

    q.ensure_group("sms")
    q.ensure_group("sms")  # second call must not raise


def test_queue_length(fake_redis):
    from app import queue as q

    q.publish("sms", "n-5", "g-1", "+919887270348", attempt=1)
    q.publish("sms", "n-6", "g-1", "+919887270348", attempt=1)
    assert q.queue_length("sms") == 2


def test_stream_names():
    from app import queue as q

    assert q.stream_name("sms") == "notifications:sms"
    assert q.retry_stream_name() == "notifications:retry"
    assert q.dlq_stream_name() == "notifications:dlq"
