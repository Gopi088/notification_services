"""Tests for audit logging (durable business/security record)."""
import uuid

import pytest


def test_audit_record_and_list(storage):
    from app.audit import list_audit, record_audit

    uid = uuid.uuid4().hex[:8]
    aid = record_audit(
        user_id=f"usr_{uid}", action="notification_created",
        notification_id="notif-1", channel="sms",
        recipient="+919887270348", status="queued",
        request_id="req_1",
    )
    assert aid is not None
    assert aid.startswith("AUD_")

    rows = list_audit(limit=10, user_id=f"usr_{uid}")
    assert len(rows) == 1
    latest = rows[0]
    assert latest["action"] == "notification_created"
    assert latest["user_id"] == f"usr_{uid}"


def test_audit_recipient_masked(storage):
    from app.audit import list_audit, record_audit

    record_audit(user_id="u", action="notification_created", recipient="+919887270348")
    rows = list_audit(limit=5)
    latest = rows[0]
    # recipient_reference is masked, not the full number
    assert "+919887270348" not in (latest.get("recipient_reference") or "")
    assert "****" in (latest.get("recipient_reference") or "")


def test_audit_filter_by_user_and_action(storage):
    from app.audit import list_audit, record_audit

    uid = uuid.uuid4().hex[:8]
    record_audit(user_id=f"usr_a_{uid}", action="notification_created")
    record_audit(user_id=f"usr_a_{uid}", action="notification_sent")
    record_audit(user_id=f"usr_b_{uid}", action="notification_created")

    rows = list_audit(user_id=f"usr_a_{uid}")
    assert all(r["user_id"] == f"usr_a_{uid}" for r in rows)
    assert len(rows) == 2

    created = list_audit(action="notification_created", limit=50)
    assert all(r["action"] == "notification_created" for r in created)
    assert len(created) >= 2


def test_audit_records_failure_reason(storage):
    from app.audit import list_audit, record_audit

    record_audit(
        user_id="u", action="notification_failed", notification_id="n",
        result="failure", failure_reason="provider 500",
    )
    rows = list_audit(limit=5)
    assert rows[0]["result"] == "failure"
    assert rows[0]["failure_reason"] == "provider 500"


def test_audit_metadata_and_provider(storage):
    from app.audit import list_audit, record_audit

    record_audit(
        user_id="u", action="notification_sent", channel="whatsapp",
        provider="vonage_whatsapp", status="submitted",
        metadata={"attempt": 1, "latency_ms": 120},
    )
    rows = list_audit(limit=5)
    r = rows[0]
    assert r["channel"] == "whatsapp"
    assert r["provider"] == "vonage_whatsapp"


def test_audit_record_failure_does_not_crash():
    """If storage is unavailable, record_audit logs an error but still returns
    a file-based audit_id (file fallback keeps audit durable)."""
    import os
    import tempfile

    from unittest.mock import patch

    from app.audit import record_audit

    with tempfile.NamedTemporaryFile(suffix=".audit", delete=False, mode="w") as tf:
        tf_path = tf.name
    os.environ["AUDIT_LOG_FILE"] = tf_path
    from app.config import get_settings

    get_settings.cache_clear()
    with patch("app.audit.get_storage", side_effect=RuntimeError("db down")):
        aid = record_audit(user_id="u", action="notification_created")
    assert aid is not None
    assert aid.startswith("AUD_")
    os.unlink(tf_path)
    os.environ["AUDIT_LOG_FILE"] = ""
    get_settings.cache_clear()


def test_endpoint_send_produces_audit(client, storage):
    """A send through the API produces notification_created audit records."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                              "message": "audit test"})
    assert r.status_code == 202
    time.sleep(0.3)
    rows = list_audit(limit=10)
    actions = [x["action"] for x in rows]
    assert "notification_created" in actions


def test_audit_file_persistence(tmp_path):
    """Audit records are written to the dedicated audit file (JSON lines)."""
    import json as _json
    import os

    from app.audit import list_audit_from_file, record_audit

    audit_file = str(tmp_path / "audit.log")
    os.environ["AUDIT_LOG_FILE"] = audit_file
    from app.config import get_settings

    get_settings.cache_clear()
    record_audit(user_id="usr_x", action="notification_created",
                 notification_id="n-1", channel="sms", status="queued",
                 request_id="req-1")

    # Re-read from the file (simulates a fresh process reading durable audit).
    rows = list_audit_from_file(limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "notification_created"
    assert rows[0]["user_id"] == "usr_x"
    assert rows[0]["request_id"] == "req-1"
    assert rows[0]["audit_id"].startswith("AUD_")
    os.environ["AUDIT_LOG_FILE"] = ""
    get_settings.cache_clear()


def test_audit_survives_restart(tmp_path):
    """Audit history remains available after 'restart' (new storage instance)."""
    import os

    from app.audit import record_audit
    from app.storage import Storage

    db = str(tmp_path / "audit.db")
    os.environ["DATABASE_PATH"] = db
    os.environ["STORAGE_BACKEND"] = "sqlite"
    from app.config import get_settings

    get_settings.cache_clear()
    # First "process" writes audit
    s1 = Storage(backend="sqlite", url=db)
    s1.connect()
    s1.init_schema()
    import app.audit as audit_mod

    audit_mod.get_storage = lambda: s1  # point audit at this storage
    audit_mod.record_audit(user_id="usr_a", action="notification_created",
                           notification_id="n-1", channel="whatsapp", status="queued")
    s1.close()

    # Second "process" (new Storage) reads the same DB file
    s2 = Storage(backend="sqlite", url=db)
    s2.connect()
    rows = s2.list_audit(limit=10)
    assert len(rows) >= 1
    assert rows[0]["action"] == "notification_created"
    assert rows[0]["user_id"] == "usr_a"
    s2.close()
    get_settings.cache_clear()


def test_audit_user_id_from_api_key(client, storage):
    """A send with an API key records the derived user identity, not None."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post(
            "/api/v1/notifications/send",
            json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                  "message": "user audit"},
            headers={"X-API-Key": "some-api-key-value"},
        )
    assert r.status_code == 202
    time.sleep(0.3)
    rows = list_audit(limit=10)
    created = [x for x in rows if x["action"] == "notification_created"]
    assert created, "notification_created audit missing"
    assert created[0]["user_id"] is not None
    assert created[0]["user_id"] != "None"
    assert created[0]["user_id"].startswith("usr_")
    # raw API key must never appear
    assert all("some-api-key-value" not in (x.get("user_id") or "") for x in rows)


def test_audit_request_id_propagation(client, storage):
    """Audit records carry the request_id propagated from the API request."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post(
            "/api/v1/notifications/send",
            json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                  "message": "req audit"},
            headers={"X-Request-ID": "REQ-CLIENT-ABC"},
        )
    assert r.status_code == 202
    time.sleep(0.3)
    rows = list_audit(limit=10)
    created = [x for x in rows if x["action"] == "notification_created"]
    assert created
    assert created[0]["request_id"] == "REQ-CLIENT-ABC"


def test_audit_recipient_masked_in_file(tmp_path):
    """Audit never stores full recipient PII, even in the audit file."""
    import json as _json
    import os

    from app.audit import list_audit_from_file, record_audit

    audit_file = str(tmp_path / "audit2.log")
    os.environ["AUDIT_LOG_FILE"] = audit_file
    from app.config import get_settings

    get_settings.cache_clear()
    record_audit(user_id="u", action="notification_created",
                 notification_id="n", channel="sms",
                 recipient="+919887270348")
    rows = list_audit_from_file(limit=5)
    assert rows
    ref = rows[0].get("recipient_reference") or ""
    assert "+919887270348" not in ref
    assert "****" in ref
    os.environ["AUDIT_LOG_FILE"] = ""
    get_settings.cache_clear()
