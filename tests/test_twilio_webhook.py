"""Tests for the Twilio delivery-status webhook (SMS + WhatsApp)."""
import uuid

import pytest


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    """Other test files leak MOCK_MODE=false into the global env; force it on
    so webhook signature validation is skipped for the 200-path tests."""
    from app.config import get_settings

    monkeypatch.setenv("MOCK_MODE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mk_notification(storage, provider_id=None, channel="whatsapp", status="submitted"):
    pv = provider_id or f"SM{uuid.uuid4().hex[:16]}"
    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel=channel, recipient="+919887270348",
        message="x", status=status,
    )
    storage.set_provider_info(nid, "twilio_whatsapp", pv)
    return nid, pv


def _post(client, form: dict):
    return client.post("/api/v1/twilio/status", data=form)


def test_twilio_webhook_delivered(client, storage):
    """Twilio 'delivered' status transitions the notification."""
    from app.audit import list_audit

    nid, pv = _mk_notification(storage)
    r = _post(client, {"MessageSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_delivered" in actions


def test_twilio_webhook_sent_then_delivered(client, storage):
    """Twilio 'sent' maps to submitted (already), then 'delivered' advances."""
    nid, pv = _mk_notification(storage)
    assert _post(client, {"MessageSid": pv, "MessageStatus": "sent"}).status_code == 200
    # "sent" maps to our "submitted" (carrier accepted == provider accepted).
    assert storage.get_notification(nid)["status"] == "submitted"
    assert _post(client, {"MessageSid": pv, "MessageStatus": "delivered"}).status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"


def test_twilio_webhook_read(client, storage):
    """Twilio 'read' (WhatsApp) transitions to read."""
    nid, pv = _mk_notification(storage)
    r = _post(client, {"MessageSid": pv, "MessageStatus": "read"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "read"


def test_twilio_webhook_failed(client, storage):
    """Twilio 'failed' transitions to failed with error info."""
    from app.audit import list_audit

    nid, pv = _mk_notification(storage)
    r = _post(client, {"MessageSid": pv, "MessageStatus": "failed",
                       "ErrorCode": "30007", "ErrorMessage": "carrier rejected"})
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "failed"
    assert "30007" in row["last_error"]
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_failed" in actions


def test_twilio_webhook_unknown_message_ignored(client, storage):
    """A callback for an unknown message id is recorded but does not crash."""
    r = _post(client, {"MessageSid": "SMdoesnotexist", "MessageStatus": "delivered"})
    assert r.status_code == 200


def test_twilio_webhook_missing_fields_ignored(client):
    r = _post(client, {"MessageStatus": "delivered"})
    assert r.status_code == 200


def test_twilio_webhook_sms_status_updates_sms_notification(client, storage):
    """Twilio SMS status callbacks (SMS SID) update the SMS notification."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = _post(client, {"SmsSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"


def test_twilio_webhook_invalid_backward_transition_kept(client, storage):
    """A delivered message cannot be moved backwards by a stale callback."""
    nid, pv = _mk_notification(storage)
    _post(client, {"MessageSid": pv, "MessageStatus": "delivered"})
    # A stale 'queued' callback must not rewind the state.
    r = _post(client, {"MessageSid": pv, "MessageStatus": "queued"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"


def test_sms_webhook_delivered(client, storage):
    """SMS webhook endpoint transitions submitted -> delivered."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = client.post("/api/v1/sms/webhook", data={"MessageSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"
    assert storage.get_notification(nid)["delivered_at"] is not None


def test_sms_webhook_failed(client, storage):
    """SMS webhook endpoint transitions submitted -> failed."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = client.post("/api/v1/sms/webhook", data={"MessageSid": pv, "MessageStatus": "failed",
                                                  "ErrorCode": "30007", "ErrorMessage": "network error"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "failed"


def test_sms_webhook_out_of_order(client, storage):
    """An older 'sent' after 'delivered' is rejected (out-of-order safe)."""
    nid, pv = _mk_notification(storage, channel="sms")
    client.post("/api/v1/sms/webhook", data={"MessageSid": pv, "MessageStatus": "delivered"})
    assert storage.get_notification(nid)["status"] == "delivered"
    # A stale 'sent' callback must not rewind.
    client.post("/api/v1/sms/webhook", data={"MessageSid": pv, "MessageStatus": "sent"})
    assert storage.get_notification(nid)["status"] == "delivered"


def test_sms_webhook_unknown_message(client):
    """An unknown message SID is recorded, not crashed."""
    r = client.post("/api/v1/sms/webhook", data={"MessageSid": "SMdoesnotexist", "MessageStatus": "delivered"})
    assert r.status_code == 200


def test_delivery_status_shared_service_sms(storage, monkeypatch):
    """The shared update_delivery_status service works for SMS transitions."""
    from app.delivery_status import update_delivery_status

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACshared")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="sms")
    storage.set_provider_info(nid, "twilio_sms", pv)
    # delivered
    update_delivery_status("twilio_sms", pv, "delivered", channel="sms")
    assert storage.get_notification(nid)["status"] == "delivered"
    assert storage.get_notification(nid)["delivered_at"] is not None
    # unknown message id
    assert update_delivery_status("twilio_sms", "SMunknown", "delivered", channel="sms") is False
    get_settings.cache_clear()


def test_delivery_status_shared_service_whatsapp_read(storage, monkeypatch):
    """The shared service handles WhatsApp delivered -> read."""
    from app.delivery_status import update_delivery_status

    monkeypatch.setenv("MOCK_MODE", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="whatsapp")
    update_delivery_status("whatsapp", pv, "delivered", channel="whatsapp")
    assert storage.get_notification(nid)["status"] == "delivered"
    update_delivery_status("whatsapp", pv, "read", channel="whatsapp")
    assert storage.get_notification(nid)["status"] == "read"
    get_settings.cache_clear()


def test_webhook_get_endpoints_acknowledge(client):
    """GET endpoints for the delivery webhooks return 200 (acknowledgement)."""
    assert client.get("/api/v1/twilio/status").status_code == 200
    assert client.get("/api/v1/sms/webhook").status_code == 200
    assert client.get("/api/v1/twilio/sms/status").status_code == 200
    assert client.get("/api/v1/twilio/whatsapp/status").status_code == 200


def test_webhook_missing_fields_ignored(client):
    """Callbacks without a MessageSid/MessageStatus are acknowledged, not 500."""
    assert client.post("/api/v1/sms/webhook", data={"MessageStatus": "delivered"}).status_code == 200
    assert client.post("/api/v1/sms/webhook", data={"MessageSid": "SM123"}).status_code == 200


def test_webhook_valid_signature_accepted(monkeypatch, client, storage):
    """In non-mock mode a valid Twilio signature is accepted and processed."""
    import base64
    import hashlib
    import hmac

    from app.config import get_settings

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-secret")
    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="sms")
    params = {"MessageSid": pv, "MessageStatus": "delivered"}
    url = "http://testserver/api/v1/twilio/sms/status"
    body = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new("tok-secret".encode(), (url + body).encode(), hashlib.sha1).digest()
    sig = base64.b64encode(digest).decode()
    r = client.post("/api/v1/twilio/sms/status", data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"
    get_settings.cache_clear()


def test_twilio_sms_status_endpoint_delivered(client, storage):
    """Dedicated SMS status endpoint transitions to delivered."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = client.post("/api/v1/twilio/sms/status", data={"MessageSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"
    assert storage.get_notification(nid)["delivered_at"] is not None


def test_twilio_sms_status_endpoint_failed(client, storage):
    """Dedicated SMS status endpoint transitions to failed with error detail."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = client.post("/api/v1/twilio/sms/status",
                    data={"MessageSid": pv, "MessageStatus": "failed",
                          "ErrorCode": "30007", "ErrorMessage": "undelivered"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "failed"
    assert "30007" in storage.get_notification(nid)["last_error"]


def test_twilio_whatsapp_status_endpoint_delivered(client, storage):
    """Dedicated WhatsApp status endpoint transitions to delivered."""
    nid, pv = _mk_notification(storage, channel="whatsapp")
    r = client.post("/api/v1/twilio/whatsapp/status", data={"MessageSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"


def test_twilio_whatsapp_status_event_type_read(client, storage):
    """WhatsApp EventType=READ transitions delivered -> read with read_at."""
    nid, pv = _mk_notification(storage, channel="whatsapp")
    r = client.post("/api/v1/twilio/whatsapp/status",
                    data={"MessageSid": pv, "MessageStatus": "delivered", "EventType": "READ"})
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "read"
    assert row["read_at"] is not None


def test_twilio_status_missing_message_sid(client):
    """A callback without MessageSid is acknowledged, not 500."""
    r = client.post("/api/v1/twilio/sms/status", data={"MessageStatus": "delivered"})
    assert r.status_code == 200


def test_twilio_status_unsupported_status_ignored(client, storage):
    """An unsupported Twilio status is acknowledged without changing state."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = client.post("/api/v1/twilio/sms/status", data={"MessageSid": pv, "MessageStatus": "unknownthing"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "submitted"


def test_twilio_whatsapp_status_endpoint_failed(client, storage):
    """Dedicated WhatsApp status endpoint transitions to failed."""
    nid, pv = _mk_notification(storage, channel="whatsapp")
    r = client.post("/api/v1/twilio/whatsapp/status",
                    data={"MessageSid": pv, "MessageStatus": "failed", "ErrorMessage": "rejected by carrier"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "failed"


def test_canonical_endpoint_whatsapp_read_via_event_type(client, storage):
    """The canonical /api/v1/twilio/status handles WhatsApp read via EventType=READ."""
    nid, pv = _mk_notification(storage, channel="whatsapp")
    r = _post(client, {"MessageSid": pv, "MessageStatus": "delivered", "EventType": "READ"})
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "read"
    assert row["read_at"] is not None


def test_canonical_endpoint_sms_queued_maps_to_submitted(client, storage):
    """Twilio 'queued' maps to our 'submitted' (provider accepted)."""
    nid, pv = _mk_notification(storage, channel="sms")
    r = _post(client, {"MessageSid": pv, "MessageStatus": "queued"})
    assert r.status_code == 200
    assert storage.get_notification(nid)["status"] == "submitted"


def test_canonical_endpoint_accepts_sms_and_whatsapp_sids(client, storage):
    """One endpoint handles both SMS (SM...) and WhatsApp (MM...) SIDs."""
    sms_nid, sms_pv = _mk_notification(storage, channel="sms")
    wa_nid, wa_pv = _mk_notification(storage, channel="whatsapp")
    assert _post(client, {"MessageSid": sms_pv, "MessageStatus": "delivered"}).status_code == 200
    assert _post(client, {"MessageSid": wa_pv, "MessageStatus": "delivered"}).status_code == 200
    assert storage.get_notification(sms_nid)["status"] == "delivered"
    assert storage.get_notification(wa_nid)["status"] == "delivered"


def test_canonical_endpoint_duplicate_webhook_idempotent(client, storage):
    """Duplicate callbacks for the same SID do not corrupt status or history."""
    nid, pv = _mk_notification(storage, channel="sms")
    assert _post(client, {"MessageSid": pv, "MessageStatus": "delivered"}).status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"
    events_before = len(storage.list_events(nid))
    assert _post(client, {"MessageSid": pv, "MessageStatus": "delivered"}).status_code == 200
    assert _post(client, {"MessageSid": pv, "MessageStatus": "failed"}).status_code == 200
    assert storage.get_notification(nid)["status"] == "delivered"
    # No duplicate delivered history entries.
    states = [e.get("to_status") for e in storage.list_events(nid)]
    assert states.count("delivered") == 1
    assert len(storage.list_events(nid)) == events_before


def test_delayed_webhook_after_poll_does_not_corrupt(client, storage, monkeypatch):
    """A delayed webhook arriving after polling already delivered the message
    is idempotent: no rewind, no duplicate history/events."""
    from unittest.mock import patch

    from app.orchestrator import poll_delivery_status

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACdelayed")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="sms")
    # 1. Poll reports delivered -> status advances.
    with patch("app.providers.twilio_provider.TwilioSMSProvider.poll_status",
               return_value="delivered"):
        poll_delivery_status(nid)
    assert storage.get_notification(nid)["status"] == "delivered"
    events_after_poll = len(storage.list_events(nid))

    # 2. A delayed/stale webhook (e.g. older 'sent' or duplicate 'delivered')
    #    must not rewind or add duplicate history.
    _post(client, {"MessageSid": pv, "MessageStatus": "sent"})
    _post(client, {"MessageSid": pv, "MessageStatus": "delivered"})
    row = storage.get_notification(nid)
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    # No new history entries were added by the stale/duplicate callbacks.
    assert len(storage.list_events(nid)) == events_after_poll
    states = [e.get("to_status") for e in storage.list_events(nid)]
    assert states.count("delivered") == 1
    get_settings.cache_clear()


def test_twilio_webhook_signature_rejected_when_not_mock(monkeypatch, client, storage):
    """When not in MOCK_MODE and the auth token is set, a bad signature is 403."""
    from app.config import get_settings

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-secret")
    get_settings.cache_clear()
    nid, pv = _mk_notification(storage)
    r = _post(client, {"MessageSid": pv, "MessageStatus": "delivered"})
    assert r.status_code == 403
    # State unchanged (rejected).
    assert storage.get_notification(nid)["status"] == "submitted"
    get_settings.cache_clear()


def test_status_poll_transitions_submitted_to_delivered(client, storage, monkeypatch):
    """On-demand status polling advances submitted -> delivered when the
    provider reports the message was delivered on the network."""
    from unittest.mock import patch

    from app.orchestrator import poll_delivery_status

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACpoll")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="sms")
    with patch("app.providers.twilio_provider.TwilioSMSProvider.poll_status",
               return_value="delivered"):
        updated = poll_delivery_status(nid)
    assert updated is not None
    row = storage.get_notification(nid)
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    get_settings.cache_clear()


def test_status_poll_ignored_for_non_pollable_provider(storage):
    """Providers without poll_status leave the state unchanged."""
    from app.orchestrator import poll_delivery_status

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="email", recipient="a@b.com",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "azure_email", "op-123")
    assert poll_delivery_status(nid) is None
    assert storage.get_notification(nid)["status"] == "submitted"


def test_status_poll_does_not_rewind_terminal_state(storage):
    """Polling never rewinds an already-delivered message."""
    from unittest.mock import patch

    from app.orchestrator import poll_delivery_status

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="x", status="delivered",
    )
    storage.set_provider_info(nid, "twilio_sms", "SM123")
    with patch("app.providers.twilio_provider.TwilioSMSProvider.poll_status",
               return_value="queued"):
        assert poll_delivery_status(nid) is None
    assert storage.get_notification(nid)["status"] == "delivered"


def test_status_poll_unmapped_status_keeps_state(storage, monkeypatch):
    """A provider status not in the mapping (e.g. 'sending') is ignored."""
    from unittest.mock import patch

    from app.orchestrator import poll_delivery_status

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACpoll")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "twilio_sms", "SM123")
    with patch("app.providers.twilio_provider.TwilioSMSProvider.poll_status",
               return_value="sending"):
        assert poll_delivery_status(nid) is None
    assert storage.get_notification(nid)["status"] == "submitted"
    get_settings.cache_clear()


def test_api_status_trigger_poll(client, storage, monkeypatch):
    """GET /status triggers an on-demand provider poll and reflects delivery."""
    from unittest.mock import patch

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACpollapi")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+17372508034")
    from app.config import get_settings

    get_settings.cache_clear()

    nid, pv = _mk_notification(storage, channel="sms")
    mid = storage.get_notification(nid)["message_id"]
    with patch("app.providers.twilio_provider.TwilioSMSProvider.poll_status",
               return_value="delivered"):
        r = client.get(f"/api/v1/notifications/{mid}/status")
    assert r.status_code == 200
    ch = r.json()["channels"][0]
    assert ch["status"] == "delivered"
    get_settings.cache_clear()


def test_status_response_includes_history_timeline(storage):
    """Status responses include the full lifecycle timeline."""
    from app.orchestrator import get_message_summary

    import uuid as _uuid

    mid = str(_uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="whatsapp", recipient="+919887270348",
        message="x", status="queued",
    )
    storage.transition(nid, "processing", actor="worker")
    storage.transition(nid, "submitted", actor="worker", provider="p", provider_message_id="pm")
    summary = get_message_summary(mid)
    assert summary is not None
    hist = summary.get("history", [])
    states = [h["status"] for h in hist]
    assert "processing" in states, f"expected processing in history: {hist}"
    assert "submitted" in states, f"expected submitted in history: {hist}"
    assert all(h.get("at") for h in hist)


def test_delivery_confirmation_field(storage, monkeypatch):
    """Status exposes how delivery confirmation is provided per provider."""
    import uuid as _uuid

    from app.orchestrator import get_message_summary

    # Twilio -> polling (no callback URL configured)
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TWILIO_STATUS_CALLBACK_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()
    mid = str(_uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="sms", recipient="+919887270348",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "twilio_sms", "SM123")
    assert get_message_summary(mid)["delivery_confirmation"] == "polling"

    # Twilio with callback URL -> webhook
    monkeypatch.setenv("TWILIO_STATUS_CALLBACK_URL", "https://x/api/v1/twilio/status")
    get_settings.cache_clear()
    assert get_message_summary(mid)["delivery_confirmation"] == "webhook"

    # Azure -> webhook
    storage.set_provider_info(nid, "azure_email", "op-1")
    assert get_message_summary(mid)["delivery_confirmation"] == "webhook"
    get_settings.cache_clear()

    # Mock mode / unknown provider -> unavailable (never faked delivered)
    monkeypatch.setenv("MOCK_MODE", "true")
    get_settings.cache_clear()
    assert get_message_summary(mid)["delivery_confirmation"] == "unavailable"
    get_settings.cache_clear()
