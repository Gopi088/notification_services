"""
Tests for sliding-window rate limiting (Phase 5).
"""
import pytest


def _get_rate_limiter():
    """Walk the ASGI middleware stack to find the RateLimitMiddleware instance."""
    from app.main import app
    from app.middleware import RateLimitMiddleware

    def _find(obj, depth=0):
        if depth > 20:
            return None
        if isinstance(obj, RateLimitMiddleware):
            return obj
        for attr in ("app", "inner"):
            child = getattr(obj, attr, None)
            if child is not None:
                result = _find(child, depth + 1)
                if result:
                    return result
        return None

    return _find(getattr(app, "middleware_stack", None))


class TestRateLimiting:
    def _clear_window(self):
        mw = _get_rate_limiter()
        if mw:
            mw._windows.clear()

    def test_first_request_allowed(self, rate_limited_auth_client):
        client, key = rate_limited_auth_client
        self._clear_window()
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "hi"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 202
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers

    def test_burst_rejected(self, rate_limited_auth_client):
        client, key = rate_limited_auth_client
        self._clear_window()
        # With 1 req/sec and 60s window, max = 60 requests.
        # Send 61 rapidly — the 61st should be rejected.
        for i in range(60):
            resp = client.post(
                "/api/v1/notifications/send",
                json={"channel": "sms", "contact": "+919999999999", "message": f"msg {i}"},
                headers={"X-API-Key": key},
            )
            assert resp.status_code == 202, f"Request {i+1} should be accepted"

        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "overflow"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limited"

    def test_non_send_endpoint_not_rate_limited(self, rate_limited_auth_client):
        client, key = rate_limited_auth_client
        self._clear_window()
        # Status endpoints are not rate-limited
        for _ in range(10):
            resp = client.get("/api/v1/health", headers={"X-API-Key": key})
            assert resp.status_code == 200

    def test_rate_limit_headers_present(self, rate_limited_auth_client):
        client, key = rate_limited_auth_client
        self._clear_window()
        resp = client.post(
            "/api/v1/notifications/send",
            json={"channel": "sms", "contact": "+919999999999", "message": "test"},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 202
        limit = int(resp.headers["X-RateLimit-Limit"])
        assert limit > 0
        remaining = int(resp.headers["X-RateLimit-Remaining"])
        assert remaining >= 0
