"""API-level tests for send / event / status / health endpoints."""
import json
from unittest.mock import patch

import pytest

from app.providers.base import ProviderResult


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize("raw_channel,canonical,contact", [
    ("sms", "sms", "+919887270348"), ("SMS", "sms", "+919887270348"), (" Sms ", "sms", "+919887270348"),
    ("email", "email", "you@example.com"), ("EMAIL", "email", "you@example.com"), ("Email", "email", "you@example.com"),
    ("whatsapp", "whatsapp", "+919887270348"), ("WhatsApp", "whatsapp", "+919887270348"), (" WHATSAPP ", "whatsapp", "+919887270348"),
])
def test_channel_case_insensitive(client, raw_channel, canonical, contact):
    """Channel input is trimmed + case-insensitive; resolves to the same channel."""
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_sms, \
         patch("app.providers.vonage_provider.VonageWhatsAppProvider.send") as fake_wa, \
         patch("app.providers.azure_provider.AzureEmailProvider.send") as fake_email:
        fake_sms.return_value = ProviderResult("vonage_sms", "m-ch-sms", "submitted")
        fake_wa.return_value = ProviderResult("vonage_whatsapp", "m-ch-wa", "submitted")
        fake_email.return_value = ProviderResult("azure_email", "m-ch-em", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": raw_channel, "contact": contact}],
                              "message": "channel case test"})
    assert r.status_code == 202
    body = r.json()
    assert body["channels"][0]["channel"] == canonical
    # The provider for the canonical channel was used.
    if canonical == "sms":
        assert fake_sms.call_count == 1
    elif canonical == "whatsapp":
        assert fake_wa.call_count == 1
    else:
        assert fake_email.call_count == 1


def test_unsupported_channel_validation_error(client):
    """An unsupported channel is a clear 422 validation error."""
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "telegram", "contact": "+919887270348"}],
                          "message": "nope"})
    assert r.status_code == 422


def test_legacy_send_duplicate_detection(client):
    """The legacy /send endpoint detects duplicates within the window."""
    import time

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-legacy-dup", "submitted")
        r1 = client.post("/send", json={"channel": "sms", "contact": "+919887270348",
                                        "message": "legacy dup"})
        time.sleep(0.2)
        r2 = client.post("/send", json={"channel": "sms", "contact": "+919887270348",
                                        "message": "legacy dup"})
        r3 = client.post("/send", json={"channel": "sms", "contact": "+919887270348",
                                        "message": "different"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    body = r2.json()
    detail = body.get("detail", body)
    assert detail.get("duplicate") is True
    assert detail.get("message") == "Message already sent recently. Resend?"
    assert detail.get("message_id") == r1.json()["message_id"]
    # Different message -> new send.
    assert r3.status_code == 202
    assert r3.json()["message_id"] != r1.json()["message_id"]
    assert fake.call_count == 2  # only first + different message sent


def test_readiness(client):
    r = client.get("/api/v1/health/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_liveness(client):
    r = client.get("/api/v1/health/liveness")
    assert r.status_code == 200


def test_send_valid_sms(client):
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                              "message": "hello"})
    assert r.status_code == 202
    body = r.json()
    assert body["success"] is True
    assert body["channels"][0]["channel"] == "sms"
    assert body["channels"][0]["status"] == "queued"


def test_send_invalid_channel(client):
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "fax", "contact": "9887270348"}],
                          "message": "hi"})
    assert r.status_code == 422


def test_send_missing_message(client):
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "9887270348"}]})
    assert r.status_code == 422


def test_send_invalid_phone(client):
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "abc"}], "message": "hi"})
    assert r.status_code in (400, 422)


def test_send_empty_channels(client):
    r = client.post("/api/v1/notifications/send", json={"channels": [], "message": "hi"})
    assert r.status_code == 422


def test_send_duplicate_channel(client):
    r = client.post(
        "/api/v1/notifications/send",
        json={"channels": [{"channel": "sms", "contact": "9887270348"},
                            {"channel": "sms", "contact": "9887270348"}],
              "message": "hi"},
    )
    assert r.status_code == 422


def test_send_whatsapp_template(client):
    with patch("app.providers.vonage_provider.VonageWhatsAppProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_whatsapp", "wm1", "submitted")
        r = client.post(
            "/api/v1/notifications/send",
            json={"channels": [{"channel": "whatsapp", "contact": "9887270348",
                                "template_name": "test_template"}],
                  "message": "hi"},
        )
    assert r.status_code == 202


def test_send_email(client):
    with patch("app.providers.azure_provider.AzureEmailProvider._send_email") as fake:
        fake.return_value = ProviderResult("azure_email", "em1", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "email", "contact": "a@b.com"}],
                              "message": "hello"})
    assert r.status_code == 202


def test_status_flow(client, storage):
    import uuid

    mid = str(uuid.uuid4())
    storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="hi", status="queued",
    )
    r = client.get(f"/api/v1/notifications/{mid}/status")
    assert r.status_code == 200
    body = r.json()
    assert body["channels"][0]["status"] == "queued"


def test_status_not_found(client):
    r = client.get("/api/v1/notifications/nonexistent/status")
    assert r.status_code == 404


def test_status_returns_retry_count_and_delivered_at(client, storage):
    """Status API exposes retry_count, delivered_at, read_at, error, provider."""
    import uuid

    mid = str(uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="hi", status="queued",
    )
    storage.transition(nid, "processing", actor="worker")
    storage.transition(nid, "submitted", actor="worker",
                       provider="vonage_sms", provider_message_id="pm-1")
    r = client.get(f"/api/v1/notifications/{mid}/status")
    assert r.status_code == 200
    ch = r.json()["channels"][0]
    assert ch["status"] == "submitted"
    assert ch["provider"] == "vonage_sms"
    assert ch["provider_message_id"] == "pm-1"
    assert ch["retry_count"] == 0
    # Delivered_at appears (None until the webhook confirms delivery).
    assert "delivered_at" in ch


def test_status_polling_is_idempotent(client, storage):
    """Repeated GET status returns the latest persisted status without
    creating duplicate records or changing state."""
    import uuid

    mid = str(uuid.uuid4())
    storage.create_notification(
        message_id=mid, channel="whatsapp", recipient="+919887270348",
        message="hi", status="submitted",
    )
    before = storage.get_notification_by_message_id(mid)
    r1 = client.get(f"/api/v1/notifications/{mid}/status")
    r2 = client.get(f"/api/v1/notifications/{mid}/status")
    r3 = client.get(f"/api/v1/notifications/{mid}/status")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    for r in (r1, r2, r3):
        ch = r.json()["channels"][0]
        assert ch["status"] == "submitted"
        assert ch["message_id"] == mid
    # State never changes from a GET (updated_at identical, no new records).
    after = storage.get_notification_by_message_id(mid)
    assert after["status"] == before["status"] == "submitted"
    assert after["updated_at"] == before["updated_at"]


def test_event_valid(client):
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post("/api/v1/notifications/event",
                        json={"event_type": "test", "data": "body",
                              "deliveries": [{"channel": "sms",
                                              "payload": {"recipient": "+919887270348",
                                                          "message": "hi"}}]})
    assert r.status_code == 202


def test_event_empty_deliveries(client):
    r = client.post("/api/v1/notifications/event",
                    json={"event_type": "test", "deliveries": []})
    assert r.status_code == 422


def test_legacy_send(client):
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        r = client.post("/send", json={"channel": "sms", "contact": "9887270348",
                                       "message": "hello"})
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_send_idempotent_replay_after_submitted(client):
    """Sending twice with same key after first completes → replay, 1 notification."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "mid-1", "submitted")
        r1 = client.post(
            "/api/v1/notifications/send",
            json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                  "message": "idem replay"},
            headers={"Idempotency-Key": "replay-key-1"},
        )
        time.sleep(0.2)
        r2 = client.post(
            "/api/v1/notifications/send",
            json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                  "message": "idem replay"},
            headers={"Idempotency-Key": "replay-key-1"},
        )
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert fake.call_count == 1  # provider called once

    created = [a for a in list_audit(limit=50) if a["action"] == "notification_created"]
    assert len(created) == 1


def test_status_after_acknowledged(client, storage):
    """Status reflects acknowledged state with timestamps."""
    import uuid

    mid = str(uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="whatsapp", recipient="+919887270348",
        message="hi", status="delivered",
    )
    storage.mark_read(nid)
    storage.mark_acknowledged(nid, ack_type="reply", ack_message="YES", ack_source="inbound")
    r = client.get(f"/api/v1/notifications/{mid}/status")
    assert r.status_code == 200
    ch = r.json()["channels"][0]
    assert ch["status"] == "acknowledged"
    assert ch["acknowledged_at"] is not None


def test_send_provider_permanent_failure_marks_failed(client):
    """A non-retryable provider error in the background path marks failed."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderError

    with patch("app.providers.vonage_provider.VonageSMSProvider.send",
               side_effect=ProviderError("bad recipient", retryable=False, error_code="400")):
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                              "message": "fail perm"})
    assert r.status_code == 202
    # Background task runs after response; poll the status until it settles.
    gid = r.json()["message_id"]
    final = None
    for _ in range(20):
        time.sleep(0.2)
        st = client.get(f"/api/v1/notifications/{gid}/status").json()
        final = st["channels"][0]["status"]
        if final in ("failed", "delivered", "submitted"):
            break
    assert final == "failed", f"expected failed, got {final}"


def test_send_provider_unexpected_error_marks_failed(client):
    """An unexpected provider exception is captured, not propagated."""
    import time
    from unittest.mock import patch

    with patch("app.providers.vonage_provider.VonageSMSProvider.send",
               side_effect=RuntimeError("boom")):
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                              "message": "unexpected"})
    assert r.status_code == 202
    gid = r.json()["message_id"]
    final = None
    for _ in range(20):
        time.sleep(0.2)
        st = client.get(f"/api/v1/notifications/{gid}/status").json()
        final = st["channels"][0]["status"]
        if final in ("failed", "delivered", "submitted"):
            break
    assert final == "failed", f"expected failed, got {final}"


def test_accidental_duplicate_returns_existing(client):
    """Same idempotency key, no resend flag → return existing, do NOT resend."""
    import time
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-1", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "dupe check"},
                         headers={"Idempotency-Key": "dupe-key-1"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "dupe check"},
                         headers={"Idempotency-Key": "dupe-key-1"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert fake.call_count == 1  # provider called once only


def test_duplicate_response_has_clear_message_and_flag(client):
    """Accidental duplicate returns duplicate=true + guidance + existing id."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-dup", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "duplicate msg"},
                         headers={"Idempotency-Key": "dup-clear-key"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "duplicate msg"},
                         headers={"Idempotency-Key": "dup-clear-key"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    body = r2.json()
    assert body["duplicate"] is True
    assert body["message"] == "Message already sent recently. Resend?"
    # The existing message_id is returned, not a new one.
    assert body["message_id"] == r1.json()["channels"][0]["message_id"]
    assert fake.call_count == 1  # provider still called only once


def test_force_resend_alias_creates_new(client):
    """force_resend:true behaves like resend:true - creates a NEW notification."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-fr", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "force resend me"},
                         headers={"Idempotency-Key": "force-resend-key"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "force resend me", "force_resend": True},
                         headers={"Idempotency-Key": "force-resend-key"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is False
    assert r2.json()["message_id"] != r1.json()["message_id"]
    assert fake.call_count == 2  # provider called again for the resend


def test_duplicate_audit_records_correlation_fields(client):
    """duplicate_attempted audit rows carry request_id/message_id/user_id/channel."""
    import time

    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-aud", "submitted")
        client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "audit dup"},
                    headers={"Idempotency-Key": "audit-dup-key", "X-Request-ID": "req_dup_first"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "audit dup"},
                         headers={"Idempotency-Key": "audit-dup-key", "X-Request-ID": "req_dup_second"})
    assert r2.status_code == 202
    dups = [a for a in list_audit(limit=50) if a["action"] == "duplicate_attempted"]
    assert dups, "expected duplicate_attempted audit events"
    rec = dups[-1]
    assert rec["request_id"] == "req_dup_second"
    assert rec["notification_id"]
    assert rec["channel"] == "sms"
    assert "audit dup" not in json.dumps(rec)  # message content never logged


def test_duplicate_within_window_blocks(client):
    """Same content sent again within DUPLICATE_WINDOW_MINUTES is a duplicate."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-w1", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "windowed duplicate"},
                         headers={"X-API-Key": "win-user-1"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "windowed duplicate"},
                         headers={"X-API-Key": "win-user-1"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    body = r2.json()
    assert body["duplicate"] is True
    assert body["message"] == "Message already sent recently. Resend?"
    # Same existing message_id returned - no new notification created.
    assert body["message_id"] == r1.json()["channels"][0]["message_id"]
    assert fake.call_count == 1  # provider NOT called again


def test_duplicate_after_window_sends_normally(client, monkeypatch):
    """Same content sent AFTER the window is a NEW notification."""
    import datetime
    import time

    from unittest.mock import patch

    from app.config import get_settings
    from app.providers.base import ProviderResult
    from app.storage import get_storage

    monkeypatch.setenv("DUPLICATE_WINDOW_MINUTES", "30")
    get_settings.cache_clear()

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-w2", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "old duplicate"},
                         headers={"X-API-Key": "win-user-2"})
        time.sleep(0.2)
        # Backdate the created notification so it is older than the window.
        s = get_storage()
        orig_mid = r1.json()["channels"][0]["message_id"]
        old_ts = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=120)).isoformat()
        with s._sqlite() as conn:
            conn.execute("UPDATE notifications SET created_at=? WHERE message_id=?",
                         (old_ts, orig_mid))
        # Second send - outside the 30-minute window -> new notification.
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "old duplicate"},
                         headers={"X-API-Key": "win-user-2"})
    assert r2.status_code == 202
    body = r2.json()
    assert body["duplicate"] is False
    assert body["message_id"] != r1.json()["message_id"]
    assert fake.call_count == 2  # provider called again for the new send


def test_duplicate_resend_true_creates_new(client):
    """resend=true bypasses the window and creates a NEW message_id."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-w3", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "window resend"},
                         headers={"X-API-Key": "win-user-3"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "window resend", "resend": True},
                         headers={"X-API-Key": "win-user-3"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is False
    assert r2.json()["message_id"] != r1.json()["message_id"]
    assert fake.call_count == 2


def test_duplicate_independent_users(client):
    """Different users sending the same content are NOT duplicates."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-u1", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "shared content"},
                         headers={"X-API-Key": "user-alpha"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "shared content"},
                         headers={"X-API-Key": "user-beta"})
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is False
    assert r2.json()["message_id"] != r1.json()["message_id"]
    assert fake.call_count == 2


def test_duplicate_independent_recipients(client):
    """Same user+content to a different recipient is NOT a duplicate."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-r1", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "per-recipient"},
                         headers={"X-API-Key": "win-recip"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+15551234567"}],
                               "message": "per-recipient"},
                         headers={"X-API-Key": "win-recip"})
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is False
    assert fake.call_count == 2


def test_duplicate_independent_channels(client):
    """Same user+recipient+content on a different channel is NOT a duplicate."""
    import time

    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake_sms:
        fake_sms.return_value = ProviderResult("vonage_sms", "m-c1", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "per-channel"},
                         headers={"X-API-Key": "win-chan"})
        time.sleep(0.2)
        with patch("app.providers.vonage_provider.VonageWhatsAppProvider.send") as fake_wa:
            fake_wa.return_value = ProviderResult("vonage_whatsapp", "m-c2", "submitted")
            r2 = client.post("/api/v1/notifications/send",
                             json={"channels": [{"channel": "whatsapp", "contact": "+919887270348"}],
                                   "message": "per-channel"},
                             headers={"X-API-Key": "win-chan"})
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is False
    assert r2.json()["message_id"] != r1.json()["message_id"]
    assert fake_sms.call_count == 1
    assert fake_wa.call_count == 1


def test_duplicate_window_audit_events(client):
    """Window duplicate records duplicate_attempted with correlation fields."""
    import time

    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-w4", "submitted")
        client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "audit window"},
                    headers={"X-API-Key": "win-audit", "X-Request-ID": "req_waudit_1"})
        time.sleep(0.2)
        client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "audit window"},
                    headers={"X-API-Key": "win-audit", "X-Request-ID": "req_waudit_2"})
    dups = [a for a in list_audit(limit=50) if a["action"] == "duplicate_attempted"]
    assert dups, "expected duplicate_attempted audit event"
    rec = dups[-1]
    assert rec["request_id"] == "req_waudit_2"
    assert rec["notification_id"]
    assert rec["channel"] == "sms"
    assert "audit window" not in json.dumps(rec)  # message content never logged


@pytest.mark.parametrize("window_minutes", [30, 60, 120])
def test_duplicate_window_configurable(client, monkeypatch, window_minutes):
    """DUPLICATE_WINDOW_MINUTES is configurable and 30/60/120 all work."""
    import time

    from unittest.mock import patch

    from app.config import get_settings
    from app.providers.base import ProviderResult

    monkeypatch.setenv("DUPLICATE_WINDOW_MINUTES", str(window_minutes))
    get_settings.cache_clear()
    assert get_settings().DUPLICATE_WINDOW_MINUTES == window_minutes

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", f"m-{window_minutes}", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": f"window {window_minutes}"},
                         headers={"X-API-Key": f"win-{window_minutes}"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": f"window {window_minutes}"},
                         headers={"X-API-Key": f"win-{window_minutes}"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    body = r2.json()
    assert body["duplicate"] is True
    assert body["message"] == "Message already sent recently. Resend?"
    assert body["message_id"] == r1.json()["channels"][0]["message_id"]
    assert fake.call_count == 1


def test_intentional_resend_creates_new(client):
    """resend=true with existing key creates a NEW notification, keeps original."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult
    from app.storage import get_storage

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-2", "submitted")
        # First send
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "resend me"},
                         headers={"Idempotency-Key": "resend-key-1"})
        time.sleep(0.2)
        # Explicit resend
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "resend me", "resend": True},
                         headers={"Idempotency-Key": "resend-key-1"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.headers.get("X-Idempotent-Replay") is None
    assert fake.call_count == 2  # provider called again for resend

    # Both records persisted; new one linked to original
    gid1 = r1.json()["message_id"]
    gid2 = r2.json()["message_id"]
    assert gid1 != gid2

    rows = get_storage().list_audit(limit=50)
    actions = [a["action"] for a in rows]
    assert "duplicate_attempted" in actions
    assert "resend" in actions
    # Audit records carry correlation fields, never message content.
    resend_rows = [a for a in rows if a["action"] == "resend"]
    assert resend_rows
    assert resend_rows[0]["request_id"]
    assert resend_rows[0]["notification_id"]
    assert resend_rows[0]["channel"] == "sms"
    assert "resend me" not in json.dumps(resend_rows)


def test_resend_links_parent_and_increments_count(client):
    """Resend sets parent_notification_id and resend_count on the new record."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult
    from app.storage import get_storage

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-3", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "linked resend"},
                         headers={"Idempotency-Key": "link-key-1"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "linked resend", "resend": True},
                         headers={"Idempotency-Key": "link-key-1"})
        time.sleep(0.2)
        r3 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "linked resend", "resend": True},
                         headers={"Idempotency-Key": "link-key-1"})
    # Original message_id is r1's channel message id
    orig_mid = r1.json()["channels"][0]["message_id"]
    s = get_storage()
    orig_row = s.get_notification_by_message_id(orig_mid)
    assert orig_row["resend_count"] == 0

    # Find the resent rows (they have parent_notification_id == orig id)
    resends = s.get_by_parent_id(orig_row["id"], limit=50)
    assert len(resends) == 2, f"expected 2 resends linked to original, got {len(resends)}"
    for r in resends:
        assert r["resend_count"] == 1


def test_resend_after_delivered(client):
    """Resend after delivered still creates a new notification."""
    import time
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-4", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "delivered resend"},
                         headers={"Idempotency-Key": "deliv-key-1"})
        time.sleep(0.2)
        # Mark original delivered
        orig_mid = r1.json()["channels"][0]["message_id"]
        from app.storage import get_storage
        s = get_storage()
        orig_row = s.get_notification_by_message_id(orig_mid)
        s.transition(orig_row["id"], "delivered", actor="test")
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "delivered resend", "resend": True},
                         headers={"Idempotency-Key": "deliv-key-1"})
    assert r2.status_code == 202
    assert r2.headers.get("X-Idempotent-Replay") is None
    assert fake.call_count == 2


def test_resend_after_failed(client):
    """Resend after failed still creates a new notification."""
    import time
    from unittest.mock import patch

    from app.providers.base import ProviderError, ProviderResult

    calls = []

    def send_vonage(contact, message):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("bad recipient", retryable=False, error_code="400")
        return ProviderResult("vonage_sms", "m-5", "submitted")

    with patch("app.providers.vonage_provider.VonageSMSProvider.send", side_effect=send_vonage):
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "failed resend"},
                         headers={"Idempotency-Key": "fail-key-1"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "failed resend", "resend": True},
                         headers={"Idempotency-Key": "fail-key-1"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r2.headers.get("X-Idempotent-Replay") is None
    assert len(calls) == 2  # original failed, resend succeeded


def test_resend_idempotency_each_resend_unique(client):
    """Each explicit resend uses its own key - resending twice = two new sends."""
    import time
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-6", "submitted")
        r1 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "multi resend"},
                         headers={"Idempotency-Key": "multi-key-1"})
        time.sleep(0.2)
        r2 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "multi resend", "resend": True},
                         headers={"Idempotency-Key": "multi-key-1"})
        r3 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "multi resend", "resend": True},
                         headers={"Idempotency-Key": "multi-key-1"})
        r4 = client.post("/api/v1/notifications/send",
                         json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                               "message": "multi resend"},
                         headers={"Idempotency-Key": "multi-key-1"})
    # r4 (no resend flag) is a duplicate of r1, not another resend
    assert r4.headers.get("X-Idempotent-Replay") == "true"
    assert fake.call_count == 3  # original + 2 resends
