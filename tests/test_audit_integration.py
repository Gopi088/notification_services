"""Coverage for audit-instrumented branches: webhook read/unhandled, worker
audit on send, v1 rate-limit audit."""
import time
import uuid
from unittest.mock import patch

import pytest

from app.providers.base import ProviderResult


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r


def test_webhook_read_status_audit(client, storage):
    """Webhook 'read' transitions to read and records notification_read audit."""
    from app.audit import list_audit

    pv = f"wh-read-{uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "azure_whatsapp", pv)
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"data": {"channelType": "whatsapp", "messageId": pv, "status": "read"},
                           "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated"}])
    assert r.status_code == 200
    row = storage.get_by_provider_message_id(pv)
    assert row["status"] == "read"
    actions = [a["action"] for a in list_audit(limit=30)]
    assert "notification_read" in actions


def test_webhook_unhandled_status(client, storage):
    """Webhook unhandled status is recorded without crashing."""
    import uuid as _uuid

    pv = f"wh-unhandled-{_uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=str(_uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "vonage_whatsapp", pv)
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"data": {"channelType": "whatsapp", "messageId": pv, "status": "queued"},
                           "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated"}])
    assert r.status_code == 200


def test_email_delivery_report_webhook_delivered(client, storage):
    """Azure Email delivery report transitions the email to delivered and
    stamps delivered_at."""
    import uuid as _uuid

    from app.audit import list_audit

    pv = f"emailop-{_uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=str(_uuid.uuid4()), channel="email", recipient="gopi@example.com",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "azure_email", pv)
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
                           "data": {"messageId": pv, "status": "Delivered",
                                    "recipientAddress": "gopi@example.com",
                                    "senderAddress": "no-reply@example.com"}}])
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_delivered" in actions


def test_email_delivery_report_webhook_failed(client, storage):
    """Azure Email delivery report with a failed status transitions to failed."""
    import uuid as _uuid

    from app.audit import list_audit

    pv = f"emailop-fail-{_uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=str(_uuid.uuid4()), channel="email", recipient="gopi@example.com",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "azure_email", pv)
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
                           "data": {"messageId": pv, "status": "Failed",
                                    "recipientAddress": "gopi@example.com",
                                    "error": {"code": "550", "message": "mailbox full"}}}])
    assert r.status_code == 200
    row = storage.get_notification(nid)
    assert row["status"] == "failed"
    assert "550" in row["last_error"]
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_failed" in actions


def test_email_delivery_report_webhook_bounced_uses_email_endpoint(client, storage):
    """All terminal non-delivered ACS email reports become failed."""
    import uuid as _uuid

    provider_message_id = f"emailop-bounce-{_uuid.uuid4().hex[:8]}"
    notification_id = storage.create_notification(
        message_id=str(_uuid.uuid4()), channel="email", recipient="gopi@example.com",
        message="x", status="submitted",
    )
    storage.set_provider_info(notification_id, "azure_email", provider_message_id)
    response = client.post(
        "/api/v1/email/webhook",
        json=[{
            "eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
            "data": {
                "messageId": provider_message_id,
                "status": "Bounced",
                "deliveryStatusDetails": {"statusMessage": "Recipient does not exist"},
            },
        }],
    )
    assert response.status_code == 200
    row = storage.get_notification(notification_id)
    assert row["status"] == "failed"
    assert "Recipient does not exist" in row["last_error"]


def test_email_delivery_report_unknown_message_ignored(client):
    """A delivery report for an unknown operation id is recorded, not crashed."""
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
                           "data": {"messageId": "doesnotexist", "status": "Delivered"}}])
    assert r.status_code == 200


def test_worker_send_produces_sent_audit(storage, fake_redis_client):
    """Worker success path records notification_sent audit."""
    from app.audit import list_audit
    from app.worker import process_message

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="queued",
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m1", "submitted")
        ok = process_message("sms", {"notification_id": nid, "channel": "sms",
                                     "recipient": "+919887270348", "attempt": 1})
    assert ok is True
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_sent" in actions


def test_worker_deadletter_produces_audit(storage, fake_redis_client):
    """Worker non-retryable failure produces notification_failed audit."""
    from app.audit import list_audit
    from app.worker import process_message

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="queued", max_attempts=1,
    )
    from app.providers.base import ProviderError

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.side_effect = ProviderError("bad recipient", retryable=False, error_code="400")
        ok = process_message("sms", {"notification_id": nid, "channel": "sms",
                                     "recipient": "+919887270348", "attempt": 1})
    assert ok is True
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "notification_failed" in actions


def test_worker_duplicate_produces_audit(storage, fake_redis_client):
    """Worker duplicate skip produces duplicate_notification_attempted audit."""
    from app.audit import list_audit
    from app.worker import process_message

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="submitted",
    )
    storage.set_provider_info(nid, "vonage_sms", "existing-id")
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        ok = process_message("sms", {"notification_id": nid, "channel": "sms",
                                     "recipient": "+919887270348", "attempt": 2})
    assert ok is True
    fake.assert_not_called()
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "duplicate_notification_attempted" in actions


def test_api_rate_limit_audit(client, monkeypatch):
    """Rate-limit rejection is audited."""
    from app.audit import list_audit
    from app import ratelimit

    monkeypatch.setattr("app.routers.v1.check_api_send",
                        lambda k: ratelimit.RateLimitResult(False, 0, 0, 60))
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "9887270348"}],
                          "message": "hi"})
    assert r.status_code == 429
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "rate_limit_exceeded" in actions


def test_audit_email_channel(client, storage):
    """Email sends produce notification_created audit with channel=email."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.azure_provider.AzureEmailProvider._send_email") as fake:
        fake.return_value = ProviderResult("azure_email", "em-1", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "email", "contact": "a@b.com"}],
                              "message": "email audit"})
    assert r.status_code == 202
    time.sleep(0.3)
    rows = list_audit(limit=10)
    email_created = [x for x in rows if x["action"] == "notification_created" and x["channel"] == "email"]
    assert email_created


def test_audit_whatsapp_channel(client, storage):
    """WhatsApp sends produce notification_created audit with channel=whatsapp."""
    import time
    from unittest.mock import patch

    from app.audit import list_audit
    from app.providers.base import ProviderResult

    with patch("app.providers.vonage_provider.VonageWhatsAppProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_whatsapp", "wm-1", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "whatsapp", "contact": "9887270348"}],
                              "message": "wa audit"})
    assert r.status_code == 202
    time.sleep(0.3)
    rows = list_audit(limit=10)
    wa_created = [x for x in rows if x["action"] == "notification_created" and x["channel"] == "whatsapp"]
    assert wa_created


def test_audit_retry_event(storage, fake_redis_client):
    """Retryable worker failure produces retry_scheduled audit."""
    from app.audit import list_audit
    from app.worker import process_message
    from app.providers.base import ProviderError

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="queued", max_attempts=3,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.side_effect = ProviderError("timeout", retryable=True, error_code="TIMEOUT")
        ok = process_message("sms", {"notification_id": nid, "channel": "sms",
                                     "recipient": "+919887270348", "attempt": 1})
    assert ok is True
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "retry_scheduled" in actions


def test_audit_processing_and_retry_attempted(storage, fake_redis_client):
    """Worker records notification_processing and retry_attempted audits."""
    from app.audit import list_audit
    from app.worker import process_message
    from app.providers.base import ProviderError

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+919887270348",
        message="hi", status="retrying", max_attempts=3,
    )
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m2", "submitted")
        process_message("sms", {"notification_id": nid, "channel": "sms",
                                "recipient": "+919887270348", "attempt": 2})
    actions = [a["action"] for a in list_audit(limit=30)]
    assert "retry_attempted" in actions
    assert "notification_processing" in actions


def test_audit_acknowledged(storage):
    """mark_acknowledged records a notification_acknowledged audit event."""
    from app.audit import list_audit

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="x", status="delivered",
    )
    storage.mark_acknowledged(nid, ack_type="reply", ack_message="ok", ack_source="inbound")
    actions = [a["action"] for a in list_audit(limit=30)]
    assert "notification_acknowledged" in actions


def test_audit_authorization_denied():
    """Authorization denials are auditable (action exists in the event set)."""
    from app.audit import AUDIT_EVENTS

    assert "authorization_denied" in AUDIT_EVENTS
    assert "rate_limit_exceeded" in AUDIT_EVENTS
    assert "duplicate_notification_attempted" in AUDIT_EVENTS
    assert "notification_scheduled" in AUDIT_EVENTS
    assert "notification_cancelled" in AUDIT_EVENTS
