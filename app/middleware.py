"""
Request correlation middleware.

Every request gets a unique request_id (UUID4) that propagates through
contextvars to all log lines and audit records. Incoming X-Request-ID
headers are honoured. The id is echoed back in the response header.

Logs every request on arrival and completion with:
  method, path, client IP, status code, duration in ms

Also contains a sliding-window rate limiter with throttling.
"""
import asyncio
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
                "%s %s completed with status=%d in %.1ms (SLOW)",
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
# Sliding-window rate limiter with THROTTLING (per API key hash)
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter with throttling.

    - Requests within limit: processed immediately
    - Requests over limit: DELAYED (throttled) but still processed
    - Delay increases with each extra request: 0.1s, 0.2s, 0.3s...

    Each API key hash gets its own independent window.
    """

    def __init__(self, app, default_per_second: int = 10, window_seconds: int = 1):
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

    def _check_and_throttle(self, key_hash: str, limit: int) -> tuple[int, int, float]:
        """
        Returns (remaining, total_limit, delay_seconds).
        Updates the sliding window and calculates throttle delay.
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
        current_count = len(timestamps)

        # Calculate delay if over limit
        if current_count >= max_requests:
            excess = current_count - max_requests + 1
            delay = excess * 0.1  # 0.1s per extra request
        else:
            delay = 0.0

        # Record this request
        timestamps.append(now)
        remaining = max(0, max_requests - len(timestamps))

        return remaining, max_requests, delay

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only rate-limit POST /api/v1/notifications/send
        if request.method != "POST" or "/api/v1/notifications/send" not in request.url.path:
            return await call_next(request)

        from app.config import get_settings
        settings = get_settings()

        # Use API key hash if provided, otherwise use anonymous identifier
        x_api_key = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")

        if x_api_key:
            from app.database import hash_api_key
            key_hash = hash_api_key(x_api_key)
        elif authorization.startswith("Bearer "):
            # Extract client name from JWT token for rate limiting
            try:
                import jwt
                token = authorization[7:]
                payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
                key_hash = f"oauth:{payload.get('sub', 'unknown')}"
            except Exception:
                key_hash = "anonymous"
        else:
            key_hash = "anonymous"

        limit = self._get_limit(key_hash)
        remaining, total_limit, delay = self._check_and_throttle(key_hash, limit)

        # Apply throttle delay if needed
        if delay > 0:
            logger.warning(
                "Throttling request: key=%s delay=%.1fs (over limit by %d)",
                key_hash[:16], delay, int(delay / 0.1),
                extra={"method": "POST", "path": "/api/v1/notifications/send",
                       "status_code": 200, "duration_ms": delay * 1000},
            )
            await asyncio.sleep(delay)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(total_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Throttled"] = "true" if delay > 0 else "false"
        if delay > 0:
            response.headers["X-RateLimit-Delay"] = f"{delay:.1f}s"
        return response
