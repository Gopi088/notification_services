"""Tests for the candidate communication report API."""
import uuid


def _mk(client, channel, recipient=None, status="delivered", **extra):
    import json
    from unittest.mock import patch

    from app.providers.base import ProviderResult

    providers = {
        "sms": ("app.providers.vonage_provider.VonageSMSProvider.send", "vonage_sms"),
        "whatsapp": ("app.providers.vonage_provider.VonageWhatsAppProvider.send", "vonage_whatsapp"),
        "email": ("app.providers.azure_provider.AzureEmailProvider.send", "azure_email"),
    }
    path, provider = providers[channel]
    if recipient is None:
        recipient = "cand@example.com" if channel == "email" else "+919887270348"
    contact = extra.pop("contact", recipient)
    with patch(path) as fake:
        fake.return_value = ProviderResult(provider, f"pm-{uuid.uuid4().hex[:8]}", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": channel, "contact": contact}],
                              "message": f"report {channel}"})
    return r, contact


def test_report_empty_candidate(client):
    r = client.get("/api/v1/reports/candidates/nobody@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["total_messages"] == 0
    assert body["messages"] == []
    assert body["by_channel"] == {}


def test_report_single_email(client):
    r, contact = _mk(client, "email")
    assert r.status_code == 202
    rep = client.get(f"/api/v1/reports/candidates/{contact}").json()
    assert rep["total_messages"] == 1
    assert rep["by_channel"] == {"email": 1}
    assert rep["messages"][0]["channel"] == "email"
    assert rep["messages"][0]["contact"] == contact


def test_report_single_sms(client):
    r, contact = _mk(client, "sms")
    rep = client.get(f"/api/v1/reports/candidates/{contact}").json()
    assert rep["total_messages"] == 1
    assert rep["by_channel"] == {"sms": 1}
    assert rep["messages"][0]["status"] in ("queued", "submitted", "delivered")


def test_report_single_whatsapp(client):
    r, contact = _mk(client, "whatsapp")
    rep = client.get(f"/api/v1/reports/candidates/{contact}").json()
    assert rep["total_messages"] == 1
    assert rep["by_channel"] == {"whatsapp": 1}


def test_report_multiple_channels(client):
    """A candidate messaged on several channels is grouped by channel."""
    _mk(client, "sms", "+919999999999")
    _mk(client, "whatsapp", "+919999999999")
    _mk(client, "email", "cand@example.com")
    rep = client.get("/api/v1/reports/candidates/+919999999999").json()
    assert rep["total_messages"] == 2
    assert rep["by_channel"] == {"sms": 1, "whatsapp": 1}


def test_report_delivered_read_timestamps_and_retry(client, storage):
    """Report surfaces delivered_at/read_at/retry_count/error fields."""
    mid = str(uuid.uuid4())
    nid = storage.create_notification(
        message_id=mid, channel="whatsapp", recipient="+911112223333",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "vonage_whatsapp", "pm-read-1")
    storage.transition(nid, "delivered", actor="webhook")
    storage.transition(nid, "read", actor="webhook")
    rep = client.get("/api/v1/reports/candidates/+911112223333").json()
    msg = rep["messages"][0]
    assert msg["status"] == "read"
    assert msg["read_at"] is not None
    assert msg["delivered_at"] is not None


def test_report_failed_and_retry(client, storage):
    """Failed messages include error info and retry_count."""
    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="sms", recipient="+915555666777",
        message="x", status="queued",
    )
    storage.transition(nid, "processing", actor="worker")
    storage.transition(nid, "retrying", actor="worker")   # retry_count + 1
    storage.transition(nid, "processing", actor="worker")  # retry attempt
    storage.transition(nid, "failed", actor="worker", error="carrier rejected")
    rep = client.get("/api/v1/reports/candidates/+915555666777").json()
    msg = rep["messages"][0]
    assert msg["status"] == "failed"
    assert msg["error"] is not None
    assert msg["retry_count"] >= 1


def test_report_resend_records(client, storage):
    """Resends create separate records with resend_count/group info."""
    gid = str(uuid.uuid4())
    for i in range(2):
        storage.create_notification(
            message_id=str(uuid.uuid4()), channel="sms", recipient="+918888877777",
            message="x", status="delivered", group_id=gid,
            resend_count=1 if i else 0,
        )
    rep = client.get("/api/v1/reports/candidates/+918888877777").json()
    assert rep["total_messages"] == 2
    assert any(m["resend_count"] == 1 for m in rep["messages"])


def test_report_pagination(client, storage):
    for i in range(5):
        storage.create_notification(
            message_id=str(uuid.uuid4()), channel="sms", recipient="+917777888999",
            message=f"x{i}", status="delivered",
        )
    rep = client.get("/api/v1/reports/candidates/+917777888999?limit=2&offset=0").json()
    assert rep["total_messages"] == 5
    assert len(rep["messages"]) == 2
    rep2 = client.get("/api/v1/reports/candidates/+917777888999?limit=2&offset=2").json()
    assert len(rep2["messages"]) == 2
    assert rep["messages"][0]["message_id"] != rep2["messages"][0]["message_id"]


def test_report_invalid_candidate_id(client):
    r = client.get("/api/v1/reports/candidates/%20%20")
    assert r.status_code == 200
    assert r.json()["total_messages"] == 0


def test_report_requires_auth_when_enabled(monkeypatch, client):
    """The report endpoint requires a Bearer token when auth is enabled."""
    from app.config import get_settings

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("AUTH_CLIENT_ID", "dev")
    monkeypatch.setenv("AUTH_CLIENT_SECRET", "pass")
    get_settings.cache_clear()
    assert client.get("/api/v1/reports/candidates/x@y.z").status_code == 401
    tok = client.post("/api/v1/auth/login",
                      json={"client_id": "dev", "client_secret": "pass"}).json()["access_token"]
    assert client.get("/api/v1/reports/candidates/x@y.z",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    get_settings.cache_clear()