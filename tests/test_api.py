"""API-level tests for send / event / status / health endpoints."""
from unittest.mock import patch

from app.providers.base import ProviderResult


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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
    assert "notification_resend_requested" in actions
    assert "notification_resent" in actions


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
