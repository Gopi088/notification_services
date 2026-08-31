"""Tests for the storage layer (SQLite backend) + state machine."""

import pytest

from app.storage import (
    CANCELLED,
    DEAD_LETTERED,
    DELIVERED,
    FAILED,
    PROCESSING,
    QUEUED,
    RETRYING,
    SUBMITTED,
    TRANSITIONS,
    get_storage,
)


def _create(storage, status=QUEUED):
    import uuid
    return storage.create_notification(
        message_id=f"msg-{uuid.uuid4().hex[:8]}", channel="sms", recipient="+919887270348",
        message="hello", status=status,
    )


def test_create_and_get(storage):
    nid = _create(storage)
    row = storage.get_notification(nid)
    assert row is not None
    assert row["channel"] == "sms"
    assert row["status"] == QUEUED
    assert row["message_id"].startswith("msg-")
    assert row["max_attempts"] >= 1


def test_get_missing(storage):
    assert storage.get_notification("nope") is None
    assert storage.get_notification_by_message_id("nope") is None


def test_lookup_by_message_id_and_provider_id(storage):
    import uuid

    mid = f"msg-{uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="hello", status=QUEUED,
    )
    by_msg = storage.get_notification_by_message_id(mid)
    assert by_msg["id"] == nid
    storage.set_provider_info(nid, "vonage_sms", "pv-1")
    by_prov = storage.get_by_provider_message_id("pv-1")
    assert by_prov["id"] == nid


def test_state_machine_valid_transitions(storage):
    nid = _create(storage)
    assert storage.transition(nid, PROCESSING, actor="w")["status"] == PROCESSING
    assert storage.transition(nid, SUBMITTED, actor="w", provider="x", provider_message_id="m1")["status"] == SUBMITTED
    assert storage.transition(nid, DELIVERED, actor="webhook")["status"] == DELIVERED


def test_state_machine_invalid_transition_blocked(storage):
    nid = _create(storage, status=DELIVERED)
    # delivered is terminal; queued->... from delivered is invalid
    row = storage.transition(nid, PROCESSING, actor="w")
    assert row["status"] == DELIVERED  # unchanged


def test_retry_count_increments(storage):
    nid = _create(storage)
    storage.transition(nid, PROCESSING, actor="w")
    storage.transition(nid, FAILED, actor="w", error="boom")
    row = storage.transition(nid, RETRYING, actor="w")
    assert row["status"] == RETRYING
    assert row["retry_count"] == 1


def test_attempts_and_events_recorded(storage):
    nid = _create(storage)
    storage.transition(nid, PROCESSING, actor="w")
    storage.add_attempt(nid, 1, SUBMITTED, provider="x", provider_message_id="m1", duration_ms=10)
    attempts = storage.list_attempts(nid)
    assert len(attempts) == 1
    assert attempts[0]["provider"] == "x"


def test_group_lookup(storage):
    nid1 = storage.create_notification(message_id="a", channel="sms", recipient="+1", message="x", status=QUEUED, group_id="g1")
    nid2 = storage.create_notification(message_id="b", channel="sms", recipient="+1", message="y", status=QUEUED, group_id="g1")
    rows = storage.get_group("g1")
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {nid1, nid2}


def test_idempotency_key_store_and_lookup(storage):
    nid = _create(storage)
    ok = storage.store_idempotency_key("key-1", nid, "hash1")
    assert ok is True
    # duplicate insert fails
    assert storage.store_idempotency_key("key-1", "other", "hash2") is False
    found = storage.find_by_idempotency_key("key-1")
    assert found is not None
    assert found["id"] == nid


def test_due_notifications(storage):
    _create(storage)  # queued, no next_attempt_at -> due
    due = storage.due_notifications()
    assert len(due) >= 1


def test_webhook_event_record(storage):
    storage.record_webhook_event(
        provider="whatsapp", provider_message_id="wm1", status="delivered",
        payload={"status": "delivered"},
    )
    # no crash; idempotent
    storage.record_webhook_event(provider="whatsapp", provider_message_id="wm1", status="delivered")


def test_transitions_table_consistency():
    # Every state has an entry; terminal states allow nothing.
    for state, allowed in TRANSITIONS.items():
        assert isinstance(allowed, set)
    assert DELIVERED not in TRANSITIONS[DELIVERED]
    assert CANCELLED not in TRANSITIONS[CANCELLED]
    assert DEAD_LETTERED not in TRANSITIONS[DEAD_LETTERED]


def test_mark_read_transition(storage):
    """delivered -> read via mark_read."""
    nid = storage.create_notification(
        message_id="read-1", channel="whatsapp", recipient="+919887270348",
        message="hi", status="delivered",
    )
    row = storage.mark_read(nid)
    assert row is not None
    assert row["status"] == "read"
    assert row["read_at"] is not None


def test_mark_acknowledged_from_delivered(storage):
    """delivered -> acknowledged stores acknowledgement metadata."""
    nid = storage.create_notification(
        message_id="ack-1", channel="whatsapp", recipient="+919887270348",
        message="hi", status="delivered",
    )
    row = storage.mark_acknowledged(nid, ack_type="reply", ack_message="YES",
                                    ack_source="inbound_whatsapp")
    assert row is not None
    assert row["status"] == "acknowledged"
    assert row["acknowledgement_type"] == "reply"
    assert row["acknowledgement_message"] == "YES"
    assert row["acknowledged_at"] is not None


def test_mark_acknowledged_from_read(storage):
    """read -> acknowledged."""
    nid = storage.create_notification(
        message_id="ack-2", channel="whatsapp", recipient="+919887270348",
        message="hi", status="read",
    )
    row = storage.mark_acknowledged(nid, ack_type="button")
    assert row["status"] == "acknowledged"


def test_mark_acknowledged_invalid_from_queued(storage):
    """queued -> acknowledged is rejected (invalid transition)."""
    nid = storage.create_notification(
        message_id="ack-3", channel="whatsapp", recipient="+919887270348",
        message="hi", status="queued",
    )
    row = storage.mark_acknowledged(nid)
    assert row["status"] == "queued"  # unchanged


def test_transition_sets_read_at_and_acknowledged_at(storage):
    """transition() sets timestamps when entering read/acknowledged."""
    nid = storage.create_notification(
        message_id="ts-1", channel="whatsapp", recipient="+919887270348",
        message="hi", status="submitted",
    )
    r1 = storage.transition(nid, "read", actor="webhook")
    assert r1["status"] == "read"
    assert r1["read_at"] is not None
    assert r1["acknowledged_at"] is None
    r2 = storage.transition(nid, "acknowledged", actor="webhook")
    assert r2["status"] == "acknowledged"
    assert r2["acknowledged_at"] is not None


def test_submitted_to_expired(storage):
    """submitted -> expired is legal."""
    nid = storage.create_notification(
        message_id="exp-1", channel="whatsapp", recipient="+919887270348",
        message="hi", status="submitted",
    )
    row = storage.transition(nid, "expired", actor="system")
    assert row["status"] == "expired"


def test_delivered_at_set_on_delivery(storage):
    """Transitioning to delivered stamps delivered_at; nothing before."""
    import uuid

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="submitted",
    )
    row = storage.transition(nid, "delivered", actor="webhook")
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    # Backward transition delivered -> submitted is rejected and state kept.
    row2 = storage.transition(nid, "submitted", actor="webhook")
    assert row2["status"] == "delivered"


def test_status_changed_audit_recorded(storage):
    """Every transition records a status_changed audit event."""
    import uuid

    from app.audit import list_audit

    mid = str(uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="hi", status="queued",
    )
    storage.transition(nid, "processing", actor="worker")
    storage.transition(nid, "submitted", actor="worker", provider="p", provider_message_id="pm")
    rows = [a for a in list_audit(limit=50) if a["action"] == "status_changed"]
    assert rows, "expected status_changed audit events"
    assert rows[0]["notification_id"]
    assert rows[0]["channel"] == "sms"


def test_queued_to_expired(storage):
    nid = storage.create_notification(
        message_id="exp-2", channel="whatsapp", recipient="+919887270348",
        message="hi", status="queued",
    )
    row = storage.transition(nid, "expired", actor="system")
    assert row["status"] == "expired"


def test_find_recent_by_content_hash(storage):
    import datetime

    chash = "abc123"
    mid = "dup-hash-1"
    storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="hello", content_hash=chash,
    )
    # Within the window -> found.
    found = storage.find_recent_by_content_hash(chash, window_minutes=30)
    assert found is not None
    assert found["message_id"] == mid
    # Outside the window -> not found.
    assert storage.find_recent_by_content_hash(chash, window_minutes=0) is None
    # Different content hash -> not found.
    assert storage.find_recent_by_content_hash("nope", window_minutes=30) is None


def test_find_recent_by_content_hash_respects_window(storage):
    import datetime

    chash = "old-hash-1"
    old_ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=120)).isoformat()
    nid = storage.create_notification(
        message_id="dup-hash-old", channel="sms", recipient="+919887270348",
        message="hello", content_hash=chash,
    )
    # Backdate so the notification is older than the window.
    with storage._sqlite() as conn:
        conn.execute("UPDATE notifications SET created_at=? WHERE message_id=?",
                     (old_ts, "dup-hash-old"))
    assert storage.find_recent_by_content_hash(chash, window_minutes=30) is None
    # A wider window sees it.
    assert storage.find_recent_by_content_hash(chash, window_minutes=300) is not None


def test_sqlite_wal_and_busy_timeout(storage):
    """SQLite connections use WAL journal, NORMAL sync, and 30s busy_timeout."""
    s = storage
    assert s.backend == "sqlite"
    conn = s._sqlite_connect()
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()
    assert journal == "wal", f"expected wal, got {journal}"
    assert busy == 30000, f"expected 30000, got {busy}"
    assert sync == 1, f"expected NORMAL(1), got {sync}"


def test_sqlite_connect_uses_same_config(storage):
    """Storage.connect() applies the same PRAGMA settings."""
    s = storage
    conn = s._conn
    if conn is None:
        s.connect()
        conn = s._conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_sqlite_concurrent_writes_no_lock_errors(storage):
    """Concurrent writers do not raise 'database is locked' with WAL + busy_timeout."""
    import concurrent.futures
    import uuid

    s = storage

    def _write(i):
        try:
            with s._sqlite() as conn:
                conn.execute(
                    "INSERT INTO notification_events "
                    "(notification_id, from_status, to_status, actor, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), "a", "b", f"w{i}", "2026-01-01T00:00:00+00:00"),
                )
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_write, range(200)))

    assert all(r == "ok" for r in results), f"lock errors: {[r for r in results if r != 'ok'][:5]}"


def test_sqlite_concurrent_notification_sends(client):
    """Concurrent API sends complete without database-lock errors."""
    import concurrent.futures

    # No provider patch: conftest runs with MOCK_MODE=true, so providers
    # short-circuit and never contact Twilio/Azure/Vonage.
    def _send(i):
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": f"+919600000{i:05d}"}],
                              "message": f"c{i}"})
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
        codes = list(ex.map(_send, range(60)))
    assert all(c == 202 for c in codes), f"non-202: {[c for c in codes if c != 202][:5]}"
