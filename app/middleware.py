"""
Request correlation middleware.

Every request gets a unique request_id (UUID4) that propagates through
contextvars to all log lines and audit records. Incoming X-Request-ID
headers are honoured. The id is echoed back in the response header.

Logs every request on arrival and completion with:
  method, path, client IP, status code, duration in ms

Also contains a sliding-window rate limiter keyed by API key hash.
"""
import collections
import contextvars
import logging
import time
import uuid
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

logger = logging.getLogger("app.request")


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # --- Request arriving ---
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if incoming else uuid.uuid4().hex
        request_id_var.set(rid)
        request.state.request_id = rid

        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path

        logger.info(
            "%s %s", method, path,
            extra={"method": method, "path": path, "client_ip": client_ip},
        )

        # --- Process request ---
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        status = response.status_code
        extra = {
            "method": method,
            "path": path,
            "status_code": status,
            "duration_ms": elapsed_ms,
        }

        if elapsed_ms > 1000:
            logger.warning(
                "%s %s completed with status=%d in %.1fms (SLOW)",
                method, path, status, elapsed_ms,
                extra=extra,
            )
        elif status >= 500:
            logger.error(
                "%s %s completed with status=%d in %.1fms",
                method, path, status, elapsed_ms,
                extra=extra,
            )
        elif status >= 400:
            logger.warning(
                "%s %s completed with status=%d in %.1fms",
                method, path, status, elapsed_ms,
                extra=extra,
            )
        else:
            logger.info(
                "%s %s completed with status=%d in %.1fms",
                method, path, status, elapsed_ms,
                extra=extra,
            )

        response.headers["X-Request-ID"] = rid
        return response


# ---------------------------------------------------------------------------
# Sliding-window rate limiter (per API key hash)
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter.

    Each API key hash (or "anonymous" when AUTH_ENABLED=false) gets a
    deque of timestamps.  When the number of requests in the window
    exceeds the limit, the request is rejected with 429.

    The rate limit is taken from the per-key override (api_keys table)
    or falls back to settings.RATE_LIMIT_DEFAULT_PER_SECOND.
    """

    def __init__(self, app, default_per_second: int = 10, window_seconds: int = 60):
        super().__init__(app)
        self.default_per_second = default_per_second
        self.window_seconds = window_seconds
        # key_hash -> deque of timestamps (float)
        self._windows: dict[str, collections.deque] = {}

    def _get_limit(self, key_hash: Optional[str] = None) -> int:
        """Return the per-second limit, checking the DB override first."""
        if key_hash:
            from app.database import get_api_key
            record = get_api_key(key_hash)
            if record and record.get("rate_limit_per_second"):
                return record["rate_limit_per_second"]
        return self.default_per_second

    def _check_and_record(self, key_hash: str, limit: int) -> tuple[bool, int]:
        """
        Returns (allowed, remaining).  Updates the sliding window.
        """
        now = time.monotonic()
        window_start = now - self.window_seconds

        if key_hash not in self._windows:
            self._windows[key_hash] = collections.deque()

        timestamps = self._windows[key_hash]

        # Evict entries outside the window
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()

        max_requests = limit * self.window_seconds
        if len(timestamps) >= max_requests:
            remaining = 0
            return False, remaining

        timestamps.append(now)
        remaining = max_requests - len(timestamps)
        return True, remaining

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only rate-limit POST /api/v1/notifications/send
        if request.method != "POST" or "/api/v1/notifications/send" not in request.url.path:
            return await call_next(request)

        from app.config import get_settings
        settings = get_settings()

        # Use API key hash if provided, otherwise use anonymous identifier
        x_api_key = request.headers.get("x-api-key", "")
        if x_api_key:
            from app.database import hash_api_key
            key_hash = hash_api_key(x_api_key)
        else:
            key_hash = "anonymous"
        limit = self._get_limit(key_hash)
        allowed, remaining = self._check_and_record(key_hash, limit)

        if not allowed:
            from app.audit import record as audit_record
            from starlette.responses import JSONResponse
            audit_record(
                action="rate_limit.exceeded",
                outcome="denied",
                detail={"limit_per_second": limit, "window_seconds": self.window_seconds},
            )
            logger.warning(
                "Rate limit exceeded: key_hash=%s limit=%d/sec",
                key_hash[:12], limit,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": {
                        "code": "rate_limited",
                        "message": f"Rate limit exceeded. Max {limit} requests per second.",
                        "field": None,
                    },
                },
                headers={
                    "X-RateLimit-Limit": str(limit * self.window_seconds),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit * self.window_seconds)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
