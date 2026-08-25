"""Small coverage boosters for the 90% gate."""
import os
from unittest.mock import patch

import pytest


def test_idempotency_durable_concurrent_insert(storage):
    """The 'concurrent insert lost' re-read branch (store returns False)."""
    from app.idempotency import check_durable

    with patch("app.storage.Storage.store_idempotency_key", return_value=False) as fake:
        # force the not-stored path; the re-read returns the existing row
        nid, is_new = check_durable("race-key", "hash")
        assert is_new is False
        fake.assert_called()


def test_idempotency_redis_password(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "sekret")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.idempotency import _redis

    fake = object()
    with patch("redis.Redis.from_url", return_value=fake):
        r = _redis()
        assert r is fake
    monkeypatch.delenv("REDIS_PASSWORD")
    get_settings.cache_clear()


def test_queue_publish_message_helpers(fake_redis_client):
    """Cover the _message builder (queue.py lines 28-34)."""
    from app import queue as q

    q.publish("email", "n-e", "g-e", "a@b.com", attempt=1, subject="Subj")
    q.publish("sms", "n-s", "g-s", "+919887270348", attempt=2)
    assert fake_redis_client.xlen(q.stream_name("email")) == 1
    assert fake_redis_client.xlen(q.stream_name("sms")) == 1


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r
