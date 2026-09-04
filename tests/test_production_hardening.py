"""Focused regression tests for production safety controls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from pydantic import ValidationError


def test_postgres_schema_has_unique_provider_and_public_message_ids():
    from app.storage import PG_SCHEMA

    assert "uq_notifications_message_id" in PG_SCHEMA
    assert "uq_notifications_provider_message_id" in PG_SCHEMA
    assert "idx_notifications_recipient_created" in PG_SCHEMA


def test_event_payload_rejects_empty_or_ambiguous_content():
    from app.schemas import EmailAttachment, EmailPayload, WhatsAppPayload

    with pytest.raises(ValidationError):
        WhatsAppPayload(recipient="+14155551234")
    with pytest.raises(ValidationError):
        EmailPayload(recipient="a@example.com")
    with pytest.raises(ValidationError):
        EmailAttachment(name="empty.txt")
    with pytest.raises(ValidationError):
        EmailAttachment(name="two.txt", url="https://example.com/a", content_base64="YQ==")


def test_email_addresses_and_attachment_base64_are_strictly_validated():
    from app.schemas import EmailAttachment, EmailPayload

    with pytest.raises(ValidationError):
        EmailAttachment(name="bad.txt", content_base64="not base64!")
    with pytest.raises(ValidationError):
        EmailPayload(recipient="recipient@example.com", message="hello", cc=["not-an-email"])
    with pytest.raises(ValidationError):
        EmailPayload(recipient="recipient@example.com", message="hello", bcc=["same@example.com", "SAME@example.com"])


def test_worker_processing_claim_is_atomic(storage):
    from app.storage import QUEUED

    notification_id = storage.create_notification(
        message_id="f13a48d3-b49d-4a62-9696-9a5d2d172a2b", channel="sms",
        recipient="+14155551234", message="hello", status=QUEUED,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda _: storage.claim_for_processing(notification_id, from_status=QUEUED), range(2),
        ))
    assert sum(claim is not None for claim in claims) == 1
    assert storage.get_notification(notification_id)["status"] == "processing"


def test_client_idempotency_key_rejects_a_different_payload(storage):
    from fastapi import BackgroundTasks, HTTPException, Response
    from app.routers.v1 import send
    from app.schemas import ChannelRequest, SendRequest

    class Request:
        headers = {"Idempotency-Key": "production-idempotency-conflict"}

    first = SendRequest(
        channels=[ChannelRequest(channel="sms", contact="+14155551234")], message="first",
    )
    send(first, BackgroundTasks(), Request(), Response())
    second = SendRequest(
        channels=[ChannelRequest(channel="sms", contact="+14155551234")], message="second",
    )
    with pytest.raises(HTTPException) as error:
        send(second, BackgroundTasks(), Request(), Response())
    assert error.value.status_code == 409
    assert error.value.detail["error"]["code"] == "idempotency_conflict"


def test_inbound_provider_message_id_is_deduplicated(storage):
    first = storage.record_inbound_message(
        channel="sms", from_number="+14155551234", to_number="+14155550000",
        text="reply", provider_message_id="inbound-provider-id",
    )
    second = storage.record_inbound_message(
        channel="sms", from_number="+14155551234", to_number="+14155550000",
        text="replayed reply", provider_message_id="inbound-provider-id",
    )
    assert second == first
    assert len([row for row in storage.list_inbound() if row["provider_message_id"] == "inbound-provider-id"]) == 1


def test_delivery_callbacks_reject_cross_provider_and_channel_updates(storage):
    """A guessed provider ID cannot update a different provider/channel."""
    from app.delivery_status import update_delivery_status

    notification_id = storage.create_notification(
        message_id="d0d50c50-fddb-4c4f-a165-ec844ea7df25", channel="sms",
        recipient="+14155551234", message="hello", status="submitted",
    )
    storage.set_provider_info(notification_id, "twilio_sms", "SM-security-check")

    assert update_delivery_status("vonage_sms", "SM-security-check", "delivered", channel="sms") is False
    assert update_delivery_status("twilio_sms", "SM-security-check", "delivered", channel="whatsapp") is False
    assert storage.get_notification(notification_id)["status"] == "submitted"


def test_unknown_delivery_callback_is_safely_recorded(storage):
    """Unknown receipts do not create notifications or mutate delivery state."""
    from app.delivery_status import update_delivery_status

    assert update_delivery_status("twilio_sms", "SM-unknown", "delivered", channel="sms") is False


def test_inbound_webhook_requires_secret_outside_mock_mode(monkeypatch):
    from app.config import get_settings
    from app.routers.inbound import inbound_receive

    class Request:
        headers = {}

        async def json(self):
            return {"channel": "sms", "from": "+14155551234", "text": "reply"}

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "expected-secret")
    get_settings.cache_clear()
    response = asyncio.run(inbound_receive(Request()))
    assert response.status_code == 403
    assert b'"code":"unauthorized"' in response.body
    get_settings.cache_clear()


def test_postgres_pool_exhaustion_fails_cleanly():
    from app.storage import Storage

    class ExhaustedPool:
        def getconn(self):
            raise RuntimeError("connection pool exhausted")

    storage = Storage(backend="postgres", url="postgresql://user:pass@example.test/db")
    storage._pg_pool = ExhaustedPool()
    with pytest.raises(RuntimeError, match="pool is exhausted"):
        with storage._pg():
            pass


def test_queue_errors_redact_connection_credentials(monkeypatch):
    from app import queue

    class BrokenRedis:
        def xadd(self, *args, **kwargs):
            raise RuntimeError("redis://alice:secret-password@redis.example.test/0")

    monkeypatch.setattr(queue, "_client", lambda: BrokenRedis())
    with pytest.raises(queue.QueueError) as error:
        queue.publish("sms", "notification", "group", "+14155551234")
    assert "secret-password" not in str(error.value)


def test_non_mock_startup_requires_postgres_and_redis(monkeypatch):
    from app.config import get_settings
    from app.main import on_startup

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("QUEUE_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="STORAGE_BACKEND=postgres"):
        on_startup()
    get_settings.cache_clear()


def test_postgres_startup_never_initializes_legacy_sqlite(monkeypatch):
    from app.config import get_settings
    from app.main import on_startup

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example.test:5432/notifications")
    monkeypatch.setenv("QUEUE_ENABLED", "true")
    monkeypatch.setenv("QUEUE_BACKEND", "redis")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    with patch("app.main.get_storage"), patch("app.migrate.up", return_value=0), patch("app.database.init_db") as init_db:
        on_startup()
    init_db.assert_not_called()
    get_settings.cache_clear()


def test_provider_error_sanitizer_redacts_credentials():
    from app.providers.base import sanitize_provider_error

    message = sanitize_provider_error(
        "token=abc123 password: hunter2 https://user:pass@example.com/callback"
    )
    assert "abc123" not in message
    assert "hunter2" not in message
    assert "user:pass" not in message


def test_azure_webhook_rejects_missing_shared_secret(monkeypatch):
    from app.config import get_settings
    from app.routers.webhooks import webhook_receive

    class Request:
        headers = {}

        async def json(self):
            return []

    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "expected-secret")
    get_settings.cache_clear()
    response = asyncio.run(webhook_receive(Request()))
    assert response.status_code == 403
    get_settings.cache_clear()


def test_validation_handler_uses_documented_error_envelope():
    from fastapi.exceptions import RequestValidationError
    from app.main import request_validation_error_handler

    class Request:
        method = "POST"

        class url:
            path = "/api/v1/notifications/send"

    response = asyncio.run(request_validation_error_handler(Request(), RequestValidationError([{
        "type": "missing", "loc": ("body", "message"), "msg": "Field required", "input": {},
    }])))
    assert response.status_code == 422
    assert b'"success":false' in response.body
    assert b'"code":"validation_error"' in response.body


def test_http_errors_use_documented_error_envelope():
    from fastapi import HTTPException
    from app.main import http_error_handler

    class Request:
        method = "GET"

        class url:
            path = "/api/v1/notifications/missing/status"

    response = asyncio.run(http_error_handler(Request(), HTTPException(404, "Not found")))
    assert response.status_code == 404
    assert b'"success":false' in response.body
    assert b'"code":"not_found"' in response.body
