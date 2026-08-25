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
