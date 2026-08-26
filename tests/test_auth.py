"""Tests for optional API-key authentication."""


def test_auth_disabled_allows(client):
    assert client.get("/api/v1/health").status_code == 200


def test_auth_required_with_valid_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/v1/health", headers={"X-API-Key": "secret-key"}).status_code == 200
    get_settings.cache_clear()


def test_auth_required_missing_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 401
    get_settings.cache_clear()


def test_auth_required_wrong_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "secret-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/v1/health", headers={"X-API-Key": "wrong"}).status_code == 401
    get_settings.cache_clear()


def test_auth_enabled_without_key_is_server_error(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEY", "")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/api/v1/health", headers={"X-API-Key": "x"}).status_code == 500
    get_settings.cache_clear()
