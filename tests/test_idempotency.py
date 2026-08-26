"""
Tests for idempotency (Phase 6).
"""
import uuid

import pytest


class TestIdempotency:
    def test_first_request_returns_202(self, client):
        idem_key = str(uuid.uuid4())
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["success"] is True
        assert "message_id" in body

    def test_duplicate_returns_cached_response(self, client):
        idem_key = str(uuid.uuid4())
        resp1 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp1.status_code == 202
        msg_id_1 = resp1.json()["message_id"]

        resp2 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp2.status_code == 200
        assert resp2.json()["message_id"] == msg_id_1

    def test_different_keys_independent(self, client):
        resp1 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "a"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        resp2 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "b"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert resp1.json()["message_id"] != resp2.json()["message_id"]

    def test_no_idempotency_key_always_succeeds(self, client):
        """Without the header, every request gets a new message_id."""
        ids = set()
        for _ in range(5):
            resp = client.post(
                "/api/v1/notifications/send",
                json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            )
            assert resp.status_code == 202
            ids.add(resp.json()["message_id"])
        assert len(ids) == 5

    def test_failed_request_allows_retry(self, client):
        """If the first request failed validation, a second with same key is allowed."""
        idem_key = str(uuid.uuid4())
        resp1 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "invalid", "message": ""},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp1.status_code == 422

        resp2 = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "fixed"},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp2.status_code == 202
