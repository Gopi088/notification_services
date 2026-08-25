"""
Retry policy: exponential backoff with jitter.

Per docs/07-RETRY-IDEMPOTENCY.md:

    delay_ms = min(base_delay * 2^(attempt-1), max_delay) * (1 + jitter)
"""
import random

from app.config import get_settings


def backoff_delay_ms(attempt: int) -> int:
    """
    Compute the delay before the given attempt (1-based) fires.

    attempt=1 -> base_delay
    attempt=2 -> base_delay * 2
    attempt=3 -> base_delay * 4
    ...
    capped at RETRY_MAX_DELAY_MS, then jitter ±RETRY_JITTER_RATIO applied.
    """
    settings = get_settings()
    base = settings.RETRY_BASE_DELAY_MS
    maximum = settings.RETRY_MAX_DELAY_MS
    jitter = settings.RETRY_JITTER_RATIO

    exponent = max(0, attempt - 1)
    raw = min(base * (2 ** exponent), maximum)
    low = raw * (1.0 - jitter)
    high = raw * (1.0 + jitter)
    return int(random.uniform(low, high))


def is_retryable_error(error) -> bool:
    """Best-effort classification of an exception. Providers mark retryable explicitly."""
    from app.providers.base import ProviderError

    if isinstance(error, ProviderError):
        return bool(error.retryable)
    # Network/timeout errors are retryable by default.
    return True