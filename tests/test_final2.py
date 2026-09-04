"""Final coverage batch: worker template path + retry/DLQ branches, migrate PG,
main readiness/exception, orchestrator remaining, azure whatsapp edge paths."""
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def azure_env(monkeypatch):
    os.environ["MOCK_MODE"] = "false"
    os.environ["STORAGE_BACKEND"] = "sqlite"
    os.environ["AZURE_SMS_FROM"] = "+919812345678"
    os.environ["AZURE_EMAIL_FROM"] = "noreply@example.com"
    os.environ["COMMUNICATION_SERVICES_CONNECTION_STRING"] = "endpoint=https://x.communication.azure.com/;accesskey=abc"
    os.environ["WHATSAPP_CHANNEL_ID"] = "chan-123"
    os.environ["WHATSAPP_TEMPLATE_NAME"] = "test_template"
    os.environ["WHATSAPP_TEMPLATE_LANGUAGE"] = "en"
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

from app.providers.base import ProviderError, ProviderResult


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.queue as q

    monkeypatch.setattr(q, "_client", lambda: r)
    return r


# ---------- worker: template path ----------

def test_worker_template_send(storage, fake_redis_client):
    from app.worker import process_message

    nid = storage.create_notification(
        message_id=str(uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="hi", status="queued", template_name="test_template",
        template_language="en", template_params={"body": "value"},
    )
    with patch("app.providers.vonage_provider.VonageWhatsAppProvider.send_with_template") as fake:
        fake.return_value = ProviderResult("vonage_whatsapp", "wm-1", "submitted")
        ok = process_message("whatsapp", {
            "notification_id": nid, "channel": "whatsapp",
            "recipient": "+919887270348", "attempt": 1,
        })
    assert ok is True
    assert storage.get_notification(nid)["status"] == "submitted"


# ---------- worker: retry worker loop start ----------

def test_run_retry_worker_exits_on_signal(monkeypatch):
    import app.worker as worker_mod
    from app.worker import run_retry_worker

    calls = []

    def fake_process():
        calls.append("ran")

    def fake_sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_mod, "process_retry_stream", fake_process)
    monkeypatch.setattr(worker_mod, "time",
                        type("T", (), {"sleep": staticmethod(fake_sleep)})())
    with pytest.raises(KeyboardInterrupt):
        run_retry_worker()
    assert calls == ["ran"]


# ---------- migrate: PG path ----------

def test_migrate_pg(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:testpass@localhost:5434/notifications")
    from app.config import get_settings

    get_settings.cache_clear()
    from app import migrate

    n = migrate.up()
    assert n >= 0
    get_settings.cache_clear()


# ---------- main: exception handler + readiness queue fail ----------

def test_main_unhandled_returns_500(client):
    from fastapi.testclient import TestClient

    # Directly invoke the exception handler
    from app.main import unhandled_exception_handler
    from fastapi import Request

    from unittest.mock import MagicMock

    req = MagicMock(spec=Request)
    req.url.path = "/boom"
    resp = unhandled_exception_handler(req, RuntimeError("x"))
    assert resp.status_code == 500


def test_main_readiness_queue_fail(monkeypatch):
    import os
    from types import SimpleNamespace
    from fastapi.testclient import TestClient

    os.environ["QUEUE_ENABLED"] = "true"
    os.environ["MOCK_MODE"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    from app import queue as q

    def boom(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(q, "_client", boom)
    monkeypatch.setattr(q, "queue_length", boom)
    import app.main as main

    with TestClient(main.app) as c:
        # Startup remains in isolated mock mode.  Exercise the production
        # readiness decision itself, where a configured queue outage is 503.
        monkeypatch.setattr(
            main, "get_settings",
            lambda: SimpleNamespace(QUEUE_ENABLED=True, MOCK_MODE=False),
        )
        r = c.get("/api/v1/health/readiness")
    assert r.status_code == 503
    os.environ["QUEUE_ENABLED"] = "false"
    os.environ["MOCK_MODE"] = "true"
    get_settings.cache_clear()


# ---------- orchestrator: queued overall + simulate delivery non-mock ----------

def test_orchestrator_group_overall_sent(storage):
    from app.orchestrator import get_group_summary

    gid = str(uuid.uuid4())
    storage.create_notification(message_id=str(uuid.uuid4()), channel="sms", recipient="+1",
                                message="x", status="submitted", group_id=gid)
    storage.create_notification(message_id=str(uuid.uuid4()), channel="sms", recipient="+1",
                                message="x", status="queued", group_id=gid)
    summary = get_group_summary(gid)
    assert summary["status"] == "sent"


def test_maybe_simulate_delivery_not_mock(storage, monkeypatch):
    import os

    os.environ["MOCK_MODE"] = "false"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.orchestrator import _maybe_simulate_delivery

    nid = storage.create_notification(message_id=str(uuid.uuid4()), channel="sms",
                                      recipient="+1", message="x", status="submitted")
    _maybe_simulate_delivery(nid)  # no-op, no thread
    assert storage.get_notification(nid)["status"] == "submitted"
    os.environ["MOCK_MODE"] = "true"
    get_settings.cache_clear()


# ---------- azure whatsapp: SDK exception + json error ----------

def test_azure_whatsapp_sdk_exception(azure_env):
    from app.providers.azure_provider import AzureWhatsAppProvider
    from app.providers.base import ProviderError

    provider = AzureWhatsAppProvider()
    with patch("azure.communication.messages.NotificationMessagesClient.from_connection_string",
               side_effect=RuntimeError("conn failed")):
        with pytest.raises(ProviderError):
            provider.send("9887270348", "hello")


def test_azure_email_sdk_exception(azure_env):
    from app.providers.azure_provider import AzureEmailProvider
    from app.providers.base import ProviderError

    provider = AzureEmailProvider()
    with patch("azure.communication.email.EmailClient.from_connection_string",
               side_effect=RuntimeError("conn failed")):
        with pytest.raises(ProviderError):
            provider.send("a@b.com", "hello")
