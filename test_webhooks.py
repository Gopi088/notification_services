"""
Tests for the Azure Event Grid WhatsApp webhook endpoint.

Tests two scenarios:
1. SubscriptionValidationEvent — Azure Event Grid validation handshake
2. Normal delivery-status update (delivered/failed)

Run:
    python3 test_webhooks.py          # no pytest required
    python3 -m pytest test_webhooks.py -v
"""
import sys

VALIDATION_EVENT = [
    {
        "id": "2d1781af-3a4c-4d7c-bd0c-e34b19da4e66",
        "topic": "/subscriptions/xxx/resourceGroups/yyy/providers/Microsoft.Communication/CommunicationServices/notification-communication",
        "subject": "",
        "data": {
            "validationCode": "21A3267A-E3D1-4368-9CE4-1FAD126B9515",
            "validationUrl": "https://notification-communication.india.eventgrid.azure.net/api/events?validationCode=21A3267A-E3D1-4368-9CE4-1FAD126B9515",
        },
        "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
        "metadataVersion": "1",
        "dataVersion": "2",
    }
]

DELIVERY_DELIVERED_EVENT = [
    {
        "id": "37e22279-1f5c-4f12-8b8e-1b3e3e4c5d6e",
        "topic": "/subscriptions/xxx/resourceGroups/yyy/providers/Microsoft.Communication/CommunicationServices/notification-communication",
        "subject": "whatsapp/delivery",
        "data": {
            "channelType": "whatsapp",
            "messageId": "f2d5ebe4-b592-41f7-a494-78d5cd8b68de",
            "to": "+919887270348",
            "status": "delivered",
        },
        "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated",
        "dataVersion": "1.0",
        "metadataVersion": "1",
    }
]

DELIVERY_FAILED_EVENT = [
    {
        "id": "48e33380-2a6b-5d23-9c9f-2c4d4e5f6a7b",
        "topic": "/subscriptions/xxx/resourceGroups/yyy/providers/Microsoft.Communication/CommunicationServices/notification-communication",
        "subject": "whatsapp/delivery",
        "data": {
            "channelType": "whatsapp",
            "messageId": "d42632d4-20c0-4c97-b68a-3b04e40d7056",
            "to": "+919887270348",
            "status": "Failed",
            "error": {
                "code": "131026",
                "message": "Message undeliverable - the phone number is not a WhatsApp user",
            },
        },
        "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated",
        "dataVersion": "1.0",
        "metadataVersion": "1",
    }
]

# Schema variant where error lives at the data level with errorMessage/errorCode
DELIVERY_FAILED_DATA_ERROR_LEVEL = [
    {
        "id": "5a444990-3b7c-6e34-ada0-3d5e5f6a8b9c",
        "topic": "/subscriptions/xxx/resourceGroups/yyy/providers/Microsoft.Communication/CommunicationServices/notification-communication",
        "subject": "whatsapp/delivery",
        "data": {
            "channelType": "whatsapp",
            "messageId": "9bd6356b-9c51-4d29-8f08-780e5b94a608",
            "to": "+919887270348",
            "status": "Failed",
            "errorCode": "80030",
            "errorMessage": "The message could not be delivered.",
        },
        "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated",
        "dataVersion": "1.0",
        "metadataVersion": "1",
    }
]

# Schema variant with a details array inside error
DELIVERY_FAILED_DETAILS_ARRAY = [
    {
        "id": "6b555001-4c8d-7f45-be1b-4e6f6a7b9cad",
        "topic": "/subscriptions/xxx/resourceGroups/yyy/providers/Microsoft.Communication/CommunicationServices/notification-communication",
        "subject": "whatsapp/delivery",
        "data": {
            "channelType": "whatsapp",
            "messageId": "efd82349-3b62-4913-9bcd-c0d5c20e5c5e",
            "to": "+919887270348",
            "status": "failed",
            "error": {
                "code": "131047",
                "message": "",
                "details": [
                    {"code": "131047", "message": "Message failed to send because more than 24 hours have passed since the customer last wrote to the number"}
                ],
            },
        },
        "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated",
        "dataVersion": "1.0",
        "metadataVersion": "1",
    }
]


def _make_client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_validation_event(client):
    """SubscriptionValidationEvent returns {"validationResponse": "<code>"}."""
    resp = client.post("/api/v1/whatsapp/webhook", json=VALIDATION_EVENT)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "validationResponse" in data, f"Missing validationResponse in {data}"
    assert data["validationResponse"] == "21A3267A-E3D1-4368-9CE4-1FAD126B9515"


def _seed_notification_with_provider_id(message_id: str, provider_id: str, channel: str = "whatsapp"):
    """Insert a notification into the storage layer and set its provider_message_id."""
    import uuid

    from app.storage import get_storage

    storage = get_storage()
    nid = storage.create_notification(
        message_id=message_id, channel=channel, recipient="+919887270348",
        message="test", status="submitted",
    )
    # Attach provider info directly (status stays submitted).
    storage.set_provider_info(nid, "azure_whatsapp", provider_id)
    return nid


def _cleanup_notification(message_id: str):
    import sqlite3

    from app.config import get_settings

    backend = get_settings().STORAGE_BACKEND or "sqlite"
    if backend == "sqlite":
        conn = sqlite3.connect(get_settings().DATABASE_PATH)
        conn.execute("DELETE FROM notifications WHERE message_id=?", (message_id,))
        conn.commit()
        conn.close()
    else:
        import psycopg2

        from app.config import get_settings

        conn = psycopg2.connect(get_settings().DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notifications WHERE message_id=%s", (message_id,))
        conn.commit()
        conn.close()


def test_delivery_delivered(client):
    """Delivery status delivered updates the DB."""
    from app.storage import get_storage

    provider_id = "f2d5ebe4-b592-41f7-a494-78d5cd8b68de"
    _seed_notification_with_provider_id("test-delivered-msg", provider_id)

    resp = client.post("/api/v1/whatsapp/webhook", json=DELIVERY_DELIVERED_EVENT)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"status": "ok"}

    storage = get_storage()
    row = storage.get_by_provider_message_id(provider_id)
    assert row is not None
    assert row["status"] == "delivered", f"Expected delivered, got {row['status']}"
    _cleanup_notification("test-delivered-msg")


def test_delivery_failed(client):
    """Delivery status failed updates the DB with the error reason."""
    from app.storage import get_storage

    provider_id = "d42632d4-20c0-4c97-b68a-3b04e40d7056"
    _seed_notification_with_provider_id("test-failed-msg", provider_id)

    resp = client.post("/api/v1/whatsapp/webhook", json=DELIVERY_FAILED_EVENT)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"status": "ok"}

    storage = get_storage()
    row = storage.get_by_provider_message_id(provider_id)
    assert row is not None
    assert row["status"] == "failed", f"Expected failed, got {row['status']}"
    assert "not a WhatsApp user" in row["last_error"], f"Unexpected error: {row['last_error']}"
    _cleanup_notification("test-failed-msg")


def test_delivery_failed_error_code_level(client):
    """errorCode/errorMessage at data level are extracted."""
    from app.storage import get_storage

    provider_id = "9bd6356b-9c51-4d29-8f08-780e5b94a608"
    _seed_notification_with_provider_id("test-failed-code-msg", provider_id)

    resp = client.post("/api/v1/whatsapp/webhook", json=DELIVERY_FAILED_DATA_ERROR_LEVEL)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    storage = get_storage()
    row = storage.get_by_provider_message_id(provider_id)
    assert row is not None
    assert row["status"] == "failed", f"Expected failed, got {row['status']}"
    assert "80030" in row["last_error"], f"Expected error code in {row['last_error']}"
    assert "could not be delivered" in row["last_error"], f"Unexpected error: {row['last_error']}"
    _cleanup_notification("test-failed-code-msg")


def test_delivery_failed_details_array(client):
    """error.details array message is extracted when error.message is empty."""
    from app.storage import get_storage

    provider_id = "efd82349-3b62-4913-9bcd-c0d5c20e5c5e"
    _seed_notification_with_provider_id("test-failed-details-msg", provider_id)

    resp = client.post("/api/v1/whatsapp/webhook", json=DELIVERY_FAILED_DETAILS_ARRAY)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    storage = get_storage()
    row = storage.get_by_provider_message_id(provider_id)
    assert row is not None
    assert row["status"] == "failed", f"Expected failed, got {row['status']}"
    assert "24 hours" in row["last_error"], f"Expected details message in {row['last_error']}"
    _cleanup_notification("test-failed-details-msg")


def test_extract_failure_helper(client=None):
    """Unit-test the failure extractor on all supported shapes."""
    from app.routers.webhooks import _extract_failure

    # data.error with code + message
    code, msg = _extract_failure({"error": {"code": "131026", "message": "boom"}})
    assert code == "131026" and msg == "boom"

    # data.error with empty message -> falls back to details
    code, msg = _extract_failure({"error": {"code": "131047", "message": "", "details": [{"message": "nested boom"}]}})
    assert msg == "nested boom"

    # errorCode/errorMessage at data level
    code, msg = _extract_failure({"errorCode": "80030", "errorMessage": "the message could not be delivered"})
    assert code == "80030" and msg == "the message could not be delivered"

    # bare string error
    code, msg = _extract_failure({"error": "generic failure"})
    assert msg == "generic failure"

    # no error info at all -> (None, None)
    code, msg = _extract_failure({"status": "Failed"})
    assert code is None and msg is None


# ---------------------------------------------------------------------------
# Manual run support (no pytest required)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    client = _make_client()
    tests = [
        ("validation_event", test_validation_event),
        ("delivery_delivered", test_delivery_delivered),
        ("delivery_failed", test_delivery_failed),
        ("delivery_failed_error_code_level", test_delivery_failed_error_code_level),
        ("delivery_failed_details_array", test_delivery_failed_details_array),
        ("extract_failure_helper", test_extract_failure_helper),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn(client)
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001 - test runner
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)
