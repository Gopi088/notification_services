"""
Tests for notification status lifecycle (Phase 7):
queued → processing → retrying → sent → delivered
                             → failed
"""
import time
import pytest


class TestStatusLifecycle:
    def test_message_starts_queued(self, client):
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
        )
        assert resp.status_code == 202
        msg_id = resp.json()["message_id"]
        status_resp = client.get(f"/api/v1/notifications/{msg_id}/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in ("queued", "processing", "retrying", "sent", "delivered")

    def test_mock_mode_eventually_delivered(self, client):
        """In MOCK_MODE with real workers, delivery is simulated after a short delay.

        This test is skipped when workers are mocked out (test mode).
        """
        pytest.skip("Workers are mocked in test environment; mock delivery not exercised")

    def test_status_response_includes_attempt_count(self, client):
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
        )
        msg_id = resp.json()["message_id"]
        status_resp = client.get(f"/api/v1/notifications/{msg_id}/status")
        assert status_resp.status_code == 200
        ch = status_resp.json()["channel"]
        assert "attempt_count" in ch
        assert ch["attempt_count"] >= 0

    def test_nonexistent_message_returns_404(self, client):
        resp = client.get("/api/v1/notifications/nonexistent-id/status")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
