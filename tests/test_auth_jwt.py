"""JWT authentication tests for /api/v1/* routes."""
import datetime
import json

import jwt as pyjwt
import pytest

from app.providers.base import ProviderResult

TEST_JWT_SECRET = "test-jwt-secret-key-0123456789abcdef"
WRONG_TEST_JWT_SECRET = "wrong-jwt-secret-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """Enable JWT auth with a fixed secret for the whole test file."""
    from app.config import get_settings

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    monkeypatch.setenv("AUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("AUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()


def _login(client, client_id="test-client", secret="test-secret"):
    return client.post("/api/v1/auth/login",
                       json={"client_id": client_id, "client_secret": secret})


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_success(client):
    """Valid credentials return an access token."""
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user_id"] == "client_test-client"
    # Decode to verify the claims
    claims = pyjwt.decode(body["access_token"], TEST_JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == "test-client"
    assert claims["user_id"] == "client_test-client"


def test_login_invalid_credentials(client):
    """Invalid credentials return 401 and record login_failed."""
    from app.audit import list_audit

    r = _login(client, secret="wrong")
    assert r.status_code == 401
    actions = [a["action"] for a in list_audit(limit=20)]
    assert "login_failed" in actions


def test_protected_endpoint_with_valid_token(client):
    """A valid JWT grants access to a protected endpoint."""
    from unittest.mock import patch

    token = _login(client).json()["access_token"]
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-jwt", "submitted")
        r = client.post("/api/v1/notifications/send",
                        json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                              "message": "jwt hello"},
                        headers=_auth_headers(token))
    assert r.status_code == 202
    assert r.json()["channels"][0]["channel"] == "sms"


def test_missing_token_returns_401(client):
    """No Authorization header -> 401."""
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "x"})
    assert r.status_code == 401


def test_invalid_token_returns_401(client):
    """A malformed token -> 401."""
    r = client.get("/api/v1/notifications/xyz/status", headers=_auth_headers("not.a.jwt"))
    assert r.status_code == 401


def test_wrong_secret_returns_401(client):
    """A token signed with a different secret -> 401."""
    from app.auth import create_access_token
    from app.config import get_settings

    get_settings.cache_clear()
    # Create with a different secret directly.
    import jwt as pyjwt

    import datetime as dt

    payload = {"sub": "x", "user_id": "u", "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)}
    bad = pyjwt.encode(payload, WRONG_TEST_JWT_SECRET, algorithm="HS256")
    r = client.get("/api/v1/notifications/xyz/status", headers=_auth_headers(bad))
    assert r.status_code == 401


def test_expired_token_returns_401(client):
    """An expired token -> 401."""
    import jwt as pyjwt

    import datetime as dt

    payload = {"sub": "x", "user_id": "u",
               "exp": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)}
    expired = pyjwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
    r = client.get("/api/v1/notifications/xyz/status", headers=_auth_headers(expired))
    assert r.status_code == 401


def test_health_public_when_auth_enabled(client):
    """/health and /api/v1/health stay public even when auth is enabled."""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/health").status_code == 200


def test_audit_login_and_unauthorized(client):
    """Login success and authentication failures are audited."""
    from app.audit import list_audit

    _login(client)  # success
    client.get("/api/v1/notifications/xyz/status")  # no token -> 401
    actions = [a["action"] for a in list_audit(limit=30)]
    assert "login_succeeded" in actions
    assert "authentication_failed" in actions


def test_user_id_from_jwt_used_for_audit(client):
    """The JWT user_id flows into notification audit records."""
    import time

    from unittest.mock import patch

    from app.audit import list_audit

    token = _login(client).json()["access_token"]
    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-jwt-audit", "submitted")
        client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "jwt audit"},
                    headers=_auth_headers(token))
        time.sleep(0.2)
    rows = [a for a in list_audit(limit=30) if a["action"] == "notification_created"]
    assert rows
    assert rows[0]["user_id"] == "client_test-client"


def test_concurrent_requests_with_jwt(client):
    """Concurrent authenticated sends are all accepted."""
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch

    token = _login(client).json()["access_token"]
    headers = _auth_headers(token)

    def _send(_i):
        return client.post("/api/v1/notifications/send",
                           json={"channels": [{"channel": "sms", "contact": f"+9198872703{_i:02d}"}],
                                 "message": "concurrent jwt"},
                           headers=headers)

    with patch("app.providers.vonage_provider.VonageSMSProvider.send") as fake:
        fake.return_value = ProviderResult("vonage_sms", "m-cjwt", "submitted")
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_send, range(8)))
    assert all(r.status_code == 202 for r in results)
    assert fake.call_count == 8


def test_legacy_x_api_key_fallback_still_works(client, monkeypatch):
    """When auth is enabled but a legacy X-API-Key matches, access is granted."""
    from app.config import get_settings

    monkeypatch.setenv("AUTH_API_KEY", "legacy-key")
    get_settings.cache_clear()
    r = client.post("/api/v1/notifications/send",
                    json={"channels": [{"channel": "sms", "contact": "+919887270348"}],
                          "message": "x"},
                    headers={"X-API-Key": "legacy-key"})
    assert r.status_code in (202, 422)  # auth passed; 422 if validation differs
    get_settings.cache_clear()


def test_public_health_endpoints_no_token(client):
    """/health, /api/v1/health, /docs, /openapi.json are public (no JWT)."""
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/health/liveness").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_legacy_routes_require_jwt_when_enabled(client):
    """Legacy /send and /status are protected too (no alternate-route bypass)."""
    r = client.post("/send", json={"channel": "sms", "contact": "+919887270348",
                                   "message": "x"})
    assert r.status_code == 401
    assert client.get("/status/some-id").status_code == 401
    # With a valid token the legacy route works.
    tok = _login(client).json()["access_token"]
    r2 = client.post("/send", json={"channel": "sms", "contact": "+919887270348",
                                    "message": "x"}, headers=_auth_headers(tok))
    assert r2.status_code in (202, 422)


def test_webhook_does_not_require_jwt(client, storage, monkeypatch):
    """Provider webhooks use their own validation, not client JWT."""
    import uuid as _uuid

    monkeypatch.setenv("MOCK_MODE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    pv = f"wh-jwt-{_uuid.uuid4().hex[:8]}"
    nid = storage.create_notification(
        message_id=str(_uuid.uuid4()), channel="whatsapp", recipient="+919887270348",
        message="x", status="submitted",
    )
    storage.set_provider_info(nid, "azure_whatsapp", pv)
    # No JWT header - the Azure webhook uses Event Grid validation instead.
    r = client.post("/api/v1/whatsapp/webhook",
                    json=[{"data": {"channelType": "whatsapp", "messageId": pv, "status": "delivered"},
                           "eventType": "Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated"}])
    assert r.status_code == 200
    assert storage.get_by_provider_message_id(pv)["status"] == "delivered"
    get_settings.cache_clear()
