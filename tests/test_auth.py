"""Tests for authentication (require_api_key)."""
import pytest


def test_auth_disabled_allows(client):
    # conftest sets AUTH_ENABLED=false
    r = client.get("/api/v1/health")
    assert r.status_code == 200


def test_auth_required_with_valid_key(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/health", headers={"X-API-Key": "secret-key"})
        assert r.status_code == 200
    get_settings.cache_clear()


def test_auth_required_missing_key(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/health")
        assert r.status_code == 401
    get_settings.cache_clear()


def test_auth_required_wrong_key(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/health", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401
    get_settings.cache_clear()


def test_auth_enabled_no_key_configured(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/health", headers={"X-API-Key": "x"})
        assert r.status_code == 500
    get_settings.cache_clear()


def test_health_is_public_when_auth_enabled(monkeypatch):
    """/health (root) stays public even when auth is enabled."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("auth_enabled") is True
    get_settings.cache_clear()


def test_cli_auth_uses_same_api_key(monkeypatch):
    """notification_service CLI loads AUTH_API_KEY from app settings.

    Single source of truth: the CLI and the server both read AUTH_API_KEY from
    .env. There is no separate NOTIFICATION_API_KEY.
    """
    monkeypatch.setenv("AUTH_API_KEY", "cli-test-key")
    from app.config import get_settings

    get_settings.cache_clear()
    import notification_service as cli

    # API_KEY is loaded from settings at import time; reload it.
    cli.API_KEY = cli._auth_api_key()
    assert cli.API_KEY == "cli-test-key"
    assert cli.API_KEY == get_settings().AUTH_API_KEY
    get_settings.cache_clear()


def test_cli_auth_prompt_not_needed_when_key_configured(monkeypatch, capsys):
    """When AUTH_API_KEY is configured in .env, _ensure_api_key does not prompt."""
    monkeypatch.setenv("AUTH_API_KEY", "no-prompt-key")
    from app.config import get_settings

    get_settings.cache_clear()
    import notification_service as cli

    cli.API_KEY = cli._auth_api_key()
    cli._SERVER_REQUIRES_AUTH = True
    key = cli._ensure_api_key()
    assert key == "no-prompt-key"
    out = capsys.readouterr().out
    assert "API key" not in out  # no interactive prompt
    get_settings.cache_clear()


def test_no_notification_api_key_variable(monkeypatch):
    """The separate NOTIFICATION_API_KEY mechanism is gone."""
    import notification_service as cli

    assert not hasattr(cli, "NOTIFICATION_API_KEY")
    src = open(cli.__file__).read()
    assert "NOTIFICATION_API_KEY" not in src


def test_auth_key_never_in_audit(storage, monkeypatch):
    """AUTH_API_KEY must never appear in audit records."""
    monkeypatch.setenv("AUTH_API_KEY", "super-secret-key-value")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.audit import list_audit, record_audit

    record_audit(user_id="usr_test", action="notification_created",
                 notification_id="n1", channel="sms", request_id="req-1")
    rows = list_audit(limit=5)
    assert all("super-secret-key-value" not in str(r) for r in rows)
    get_settings.cache_clear()


def test_auth_key_never_in_logs(caplog):
    """Structured logging redacts the api_key/auth keys."""
    import logging

    from app.logging_config import CorrelatedLogger

    logger = CorrelatedLogger("auth-secret-test")
    with caplog.at_level(logging.INFO):
        logger.info("sending", api_key="super-secret-key-value",
                    authorization="Bearer tok", auth="super-secret-key-value")
    assert "super-secret-key-value" not in caplog.text
    assert "Bearer tok" not in caplog.text
