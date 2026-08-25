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
