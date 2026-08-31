"""Tests for JWT authentication (replaces the legacy X-API-Key auth)."""

_PROTECTED = "/api/v1/notifications/xyz/status"


def test_auth_disabled_allows(client):
    assert client.get("/api/v1/health").status_code == 200


def test_auth_required_with_valid_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("AUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("AUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        tok = client.post("/api/v1/auth/login",
                          json={"client_id": "test-client", "client_secret": "test-secret"}).json()["access_token"]
        # Valid token reaches the route (404 = auth passed, id not found).
        assert client.get(_PROTECTED,
                          headers={"Authorization": f"Bearer {tok}"}).status_code == 404
    get_settings.cache_clear()


def test_auth_required_missing_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-0123456789abcdef")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get(_PROTECTED).status_code == 401
    get_settings.cache_clear()


def test_auth_required_invalid_token(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-0123456789abcdef")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get(_PROTECTED,
                          headers={"Authorization": "Bearer not.a.valid.token"}).status_code == 401
    get_settings.cache_clear()


def test_auth_enabled_without_secret_is_server_error(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get(_PROTECTED).status_code == 500
    get_settings.cache_clear()