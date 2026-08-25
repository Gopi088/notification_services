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
