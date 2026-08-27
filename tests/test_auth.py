"""
Tests for multi-key authentication (Phase 3) and authorization (Phase 4).
"""
import pytest


# ------------------------------------------------------------------
# Authentication tests
# ------------------------------------------------------------------

class TestAuthDisabled:
    """When AUTH_ENABLED=false, requests pass without an API key."""

    def test_send_without_key(self, client):
        resp = client.post("/api/v1/notifications/send", json={
            "channel": "sms",
            "contact": "+919999999999",
            "message": "hello",
        })
        assert resp.status_code == 202
        assert resp.json()["success"] is True

    def test_health_without_key(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When AUTH_ENABLED=true, requests must include a valid X-API-Key."""

    def test_valid_key_accepted(self, auth_client):
        client, key = auth_client
        resp = client.get("/api/v1/health", headers={"X-API-Key": key})
        assert resp.status_code == 200

    def test_missing_key_rejected(self, auth_client):
        client, _ = auth_client
        resp = client.get("/api/v1/health")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_invalid_key_rejected(self, auth_client):
        client, _ = auth_client
        resp = client.get("/api/v1/health", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_revoked_key_rejected(self, auth_client):
        client, key = auth_client
        from app.database import hash_api_key, revoke_api_key
        revoke_api_key(hash_api_key(key))
        resp = client.get("/api/v1/health", headers={"X-API-Key": key})
        assert resp.status_code == 401

    def test_expired_key_rejected(self, _isolated_db):
        import os
        os.environ["AUTH_ENABLED"] = "true"
        os.environ["MOCK_MODE"] = "true"
        os.environ["WORKER_COUNT"] = "0"
        from app.config import get_settings
        get_settings.cache_clear()

        from app.database import create_api_key, hash_api_key
        test_key = "expired-key-ccccc"
        key_hash = hash_api_key(test_key)
        create_api_key(
            key_hash=key_hash,
            name="expired-client",
            tenant_id="tenant-exp",
            scopes=["send:write"],
            expires_at="2020-01-01T00:00:00+00:00",
        )

        from unittest.mock import patch
        from app.main import app
        from fastapi.testclient import TestClient
        with patch("app.main.worker_manager"), \
             patch("app.database.reset_stale_processing", return_value=0), \
             patch("app.database.cleanup_expired_idempotency", return_value=0):
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.get("/api/v1/health", headers={"X-API-Key": test_key})
                assert resp.status_code == 401


# ------------------------------------------------------------------
# Authorization tests
# ------------------------------------------------------------------

class TestAuthorization:
    def test_send_requires_send_scope(self, readonly_auth_client):
        client, key = readonly_auth_client
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_send_with_correct_scope(self, auth_client):
        client, key = auth_client
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 202

    def test_admin_requires_admin_scope(self, readonly_auth_client):
        client, key = readonly_auth_client
        resp = client.get("/api/v1/admin/audit", headers={"X-API-Key": key})
        assert resp.status_code == 403

    def test_admin_with_correct_scope(self, auth_client):
        client, key = auth_client
        resp = client.get("/api/v1/admin/audit", headers={"X-API-Key": key})
        assert resp.status_code == 200

    def test_health_requires_no_special_scope(self, readonly_auth_client):
        client, key = readonly_auth_client
        resp = client.get("/api/v1/health", headers={"X-API-Key": key})
        assert resp.status_code == 200
