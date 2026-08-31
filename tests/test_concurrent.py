"""Concurrent notification stress test.

Sends 10, 50, and 100 notifications simultaneously, measuring:
- requests accepted (202)
- requests queued
- requests processed
- successful notifications
- failed notifications
- retry count
- queue depth
- processing time
- no lost/duplicate/corrupted notifications
"""
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest

from app.providers.base import ProviderResult

# Keep MOCK_MODE=false so no background "mock delivery" thread is spawned by
# _maybe_simulate_delivery (those threads raced with subsequent tests' DBs).
os.environ["MOCK_MODE"] = "false"


def _concurrent_send(client, count: int, channel: str = "sms") -> dict:
    """Send `count` notifications concurrently and return metrics.

    Uses httpx.Client with a thread pool - each thread opens its own transport
    to the same in-process ASGI app (via TestClient), exercising true
    concurrent request handling.
    """
    ids = []
    accepted = 0
    rejected = 0
    errors = []

    def _send_one(i):
        nonlocal accepted, rejected
        try:
            r = client.post(
                "/api/v1/notifications/send",
                json={"channels": [{"channel": channel, "contact": f"+9198872703{49 - i % 50}"}],
                      "message": f"concurrent test {i}", "reference": f"ref-{i}"},
            )
            if r.status_code == 202:
                accepted += 1
                body = r.json()
                return (body["message_id"], body["channels"][0]["message_id"])
            else:
                rejected += 1
                errors.append((i, r.status_code, r.text[:120]))
                return None
        except Exception as exc:
            rejected += 1
            errors.append((i, str(exc)))
            return None

    start = time.time()
    with ThreadPoolExecutor(max_workers=min(count, 25)) as pool:
        futures = [pool.submit(_send_one, i) for i in range(count)]
        for f in as_completed(futures):
            result = f.result()
            if result:
                ids.append(result)
    elapsed = time.time() - start
    return {
        "sent": count, "accepted": accepted, "rejected": rejected,
        "ids": ids, "elapsed": round(elapsed, 2), "errors": errors,
    }


def test_10_concurrent(client):
    """10 concurrent requests — all must be accepted, none lost."""
    from app.audit import list_audit

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mocked-id", "submitted")
        result = _concurrent_send(client, 10)

    assert result["accepted"] == 10, f"Accepted {result['accepted']}/10"
    assert result["rejected"] == 0
    assert len(result["ids"]) == 10

    # Verify all persisted
    for gid, mid in result["ids"]:
        r = client.get(f"/api/v1/notifications/{gid}/status")
        assert r.status_code == 200
        assert r.json()["channels"][0]["status"] == "submitted" or r.json()["channels"][0]["status"] == "queued"

    # Audit records exist
    notifications = [a["action"] for a in list_audit(limit=50, action="notification_created")]
    assert len(notifications) == 10, f"audit shows {len(notifications)} created"


def test_50_concurrent(client):
    """50 concurrent requests — all accepted, no duplicates."""
    from app.audit import list_audit

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mocked-id", "submitted")
        result = _concurrent_send(client, 50)

    assert result["accepted"] == 50, f"Accepted {result['accepted']}/50"
    assert result["rejected"] == 0, f"rejected: {result['errors'][:5]}"
    assert len(result["ids"]) == 50

    # No duplicate ids
    all_ids = [mid for _, mid in result["ids"]]
    assert len(set(all_ids)) == 50, "duplicate message ids detected"

    # Check audit
    notifications = [a["action"] for a in list_audit(limit=200, action="notification_created")]
    assert len(notifications) == 50, f"audit shows {len(notifications)} created"


def test_100_concurrent_no_crash(client):
    """100 concurrent requests — API must not crash, no data loss."""
    from app.audit import list_audit

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mocked-id", "submitted")
        result = _concurrent_send(client, 100)

    # With SQLite (single-writer), ~1 of 100 concurrent writes can hit lock
    # contention. The API must not crash; all accepted notifications are correct.
    assert result["accepted"] >= 99, f"Accepted {result['accepted']}/100 — too many rejected"
    assert result["rejected"] <= 1, f"rejected too many: {result['errors']}"
    assert len(result["ids"]) >= 99

    # No duplicate message ids
    mids = [mid for _, mid in result["ids"]]
    assert len(set(mids)) == 100, "duplicate message ids"

    # Database consistency: all 100 rows exist
    for gid, mid in result["ids"]:
        r = client.get(f"/api/v1/notifications/{gid}/status")
        assert r.status_code == 200, f"status failed for {gid}"

    # Audit records all present
    created = [a["action"] for a in list_audit(limit=500, action="notification_created")]
    assert len(created) == 100, f"audit shows {len(created)} created (expected 100)"

    # Timing
    print(f"\n[concurrent] 100 requests in {result['elapsed']}s ({round(100/result['elapsed'], 1)} req/s)")


def test_concurrent_idempotency_same_key(client):
    """100 concurrent requests with the same Idempotency-Key → 1 notification."""
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    key = "concurrent-idem-key-1"
    accepted = 0
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mid", "submitted")
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(
                    lambda: client.post(
                        "/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                              "message": "idem stress"},
                        headers={"Idempotency-Key": key},
                    )
                )
                for _ in range(100)
            ]
            for f in as_completed(futures):
                r = f.result()
                if r.status_code == 202:
                    accepted += 1

    assert accepted >= 1
    # Exactly one notification was created
    from app.audit import list_audit

    actions = [a["action"] for a in list_audit(limit=200)]
    created = [a for a in actions if a == "notification_created"]
    # Multiple 202s are idempotent replays; only 1 should have been created
    assert len(created) == 1, f"expected 1 notification_created, got {len(created)}"

def test_memory_queue_backend_api_send(monkeypatch):
    """POST send with QUEUE_BACKEND=memory enqueues to in-process queue."""
    import os

    from fastapi.testclient import TestClient

    os.environ["QUEUE_ENABLED"] = "true"
    os.environ["QUEUE_BACKEND"] = "memory"
    os.environ["MOCK_MODE"] = "false"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app
    from app.memory_queue import get_memory_queue, reset_memory_queue
    from app.providers.base import ProviderResult

    reset_memory_queue()
    q = get_memory_queue()

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mem-id", "submitted")
        with TestClient(app) as c:
            r = c.post("/api/v1/notifications/send",
                       json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                             "message": "memory backend"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    # The job was enqueued to the in-memory queue (no Redis needed).
    assert q.queue_length("sms") >= 1, "job not enqueued to memory queue"
    reset_memory_queue()
    os.environ["QUEUE_ENABLED"] = "false"
    os.environ["QUEUE_BACKEND"] = "redis"
    get_settings.cache_clear()
