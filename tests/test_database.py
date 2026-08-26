"""
Tests for database layer and worker resilience (Phase 1 + Phase 8).
"""
import pytest
from datetime import datetime, timedelta, timezone

from app.database import (
    cleanup_expired_idempotency,
    create_api_key,
    create_idempotency,
    create_message,
    get_api_key,
    get_idempotency,
    get_message,
    hash_api_key,
    increment_attempt,
    list_api_keys,
    reset_stale_processing,
    set_retry_schedule,
    update_idempotency,
    update_status,
)


class TestDatabaseMessages:
    def test_create_and_get(self):
        create_message("msg-1", "sms", "+919999999999", "hello", "queued")
        row = get_message("msg-1")
        assert row is not None
        assert row["channel"] == "sms"
        assert row["status"] == "queued"

    def test_update_status(self):
        create_message("msg-2", "email", "a@b.com", "hi", "queued")
        update_status("msg-2", "sent", provider="azure_email")
        row = get_message("msg-2")
        assert row["status"] == "sent"
        assert row["provider"] == "azure_email"

    def test_increment_attempt(self):
        create_message("msg-3", "sms", "+919999999999", "hi", "queued")
        increment_attempt("msg-3")
        increment_attempt("msg-3")
        row = get_message("msg-3")
        assert row["attempt_count"] == 2

    def test_set_retry_schedule(self):
        create_message("msg-4", "sms", "+919999999999", "hi", "retrying")
        next_retry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        set_retry_schedule("msg-4", next_retry)
        row = get_message("msg-4")
        assert row["next_retry_at"] is not None
        assert row["status"] == "queued"

    def test_nonexistent_returns_none(self):
        assert get_message("no-such-id") is None


class TestDatabaseAPIKeys:
    def test_create_and_get(self):
        h = hash_api_key("my-secret-key")
        create_api_key(h, "test-app", "tenant-1", ["send:write"])
        record = get_api_key(h)
        assert record is not None
        assert record["name"] == "test-app"
        assert record["tenant_id"] == "tenant-1"
        assert record["scopes"] == ["send:write"]

    def test_nonexistent_returns_none(self):
        assert get_api_key("nonexistent-hash") is None

    def test_revoke(self):
        h = hash_api_key("revoke-me")
        create_api_key(h, "revoke-app", "t", ["send:write"])
        from app.database import revoke_api_key
        assert revoke_api_key(h) is True
        assert get_api_key(h) is None

    def test_list(self):
        h = hash_api_key("list-me")
        create_api_key(h, "list-app", "t", ["send:write"])
        keys = list_api_keys()
        names = [k["name"] for k in keys]
        assert "list-app" in names


class TestDatabaseIdempotency:
    def test_create_and_get(self):
        create_idempotency("idem-1", "msg-100", "processing", ttl_hours=1)
        record = get_idempotency("idem-1")
        assert record is not None
        assert record["message_id"] == "msg-100"
        assert record["status"] == "processing"

    def test_update_status(self):
        create_idempotency("idem-2", "msg-200", "processing", ttl_hours=1)
        update_idempotency("idem-2", "completed", response_body={"success": True})
        record = get_idempotency("idem-2")
        assert record["status"] == "completed"
        assert record["response_body"]["success"] is True

    def test_nonexistent_returns_none(self):
        assert get_idempotency("no-such-key") is None

    def test_cleanup_expired(self):
        # Create an expired idempotency key
        from app.database import get_connection
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO idempotency_keys
                   (idempotency_key, message_id, status, response_body, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("expired-key", "msg-300", "processing", None,
                 "2020-01-01T00:00:00+00:00", "2020-01-01T01:00:00+00:00"),
            )
        count = cleanup_expired_idempotency()
        assert count >= 1
        assert get_idempotency("expired-key") is None


class TestWorkerResilience:
    def test_reset_stale_processing(self):
        from app.database import get_connection
        # Create a message with status=processing and old updated_at
        create_message("stale-1", "sms", "+919999999999", "hi", "processing")
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with get_connection() as conn:
            conn.execute("UPDATE messages SET updated_at = ? WHERE message_id = ?", (old_time, "stale-1"))

        reset_count = reset_stale_processing(stale_timeout_minutes=5)
        assert reset_count >= 1
        row = get_message("stale-1")
        assert row["status"] == "queued"
