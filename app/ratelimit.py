"""
Rate limiting / throttling.

Per docs/08-THROTTLING-RATE-LIMITING.md. Uses a fixed-window counter in Redis
(INCR + EXPIRE). Buckets:
  - per API key (send / status)
  - per recipient
  - per channel
  - per provider (worker egress)

Fails open when Redis is unavailable so the service keeps working.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from app.config import get_settings

logger = logging.getLogger("ratelimit")


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


def _redis():
    import redis

    settings = get_settings()
    kwargs = {"decode_responses": True}
    if settings.REDIS_PASSWORD:
        kwargs["password"] = settings.REDIS_PASSWORD
    return redis.Redis.from_url(settings.REDIS_URL, **kwargs)


def _check(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Fixed-window counter. Returns allowed + remaining/reset."""
    if not get_settings().RATELIMIT_ENABLED or limit <= 0:
        logger.debug("rate limit bypassed enabled=false")
        return RateLimitResult(True, limit, limit, 0)
    try:
        r = _redis()
        current = r.incr(key)
        if current == 1:
            r.expire(key, window_seconds)
        ttl = max(0, r.ttl(key))
        remaining = max(0, limit - current)
        allowed = current <= limit
        logger.debug("rate limit evaluated allowed=%s remaining=%d", allowed, remaining)
        if not allowed:
            logger.warning(
                "rate limit exceeded key=%s limit=%d current=%d", key, limit, current
            )
        return RateLimitResult(allowed, limit, remaining, ttl)
    except Exception as exc:  # noqa: BLE001 - fail open
        logger.warning("rate limit check failed (fail-open): %s", exc)
        return RateLimitResult(True, limit, limit, 0)


def check_api_send(api_key_id: Optional[str]) -> RateLimitResult:
    s = get_settings()
    key = f"rl:key:{api_key_id or 'anon'}:send"
    return _check(key, s.RATE_LIMIT_PER_KEY, s.RATE_LIMIT_PER_KEY_WINDOW_SECONDS)


def check_api_status(api_key_id: Optional[str]) -> RateLimitResult:
    s = get_settings()
    key = f"rl:key:{api_key_id or 'anon'}:status"
    # status reads are lighter; use a 3x higher allowance
    return _check(key, s.RATE_LIMIT_PER_KEY * 3, s.RATE_LIMIT_PER_KEY_WINDOW_SECONDS)


def check_recipient(recipient: str) -> RateLimitResult:
    s = get_settings()
    return _check(
        f"rl:recipient:{recipient}",
        s.RATE_LIMIT_PER_RECIPIENT,
        s.RATE_LIMIT_PER_RECIPIENT_WINDOW_SECONDS,
    )


def check_channel(channel: str) -> RateLimitResult:
    s = get_settings()
    return _check(
        f"rl:channel:{channel}:send",
        s.RATE_LIMIT_PER_CHANNEL,
        s.RATE_LIMIT_PER_CHANNEL_WINDOW_SECONDS,
    )


def check_provider(provider: str, limit: int, window_seconds: int = 60) -> RateLimitResult:
    return _check(f"rl:provider:{provider}:send", limit, window_seconds)
