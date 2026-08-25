"""
Idempotency support.

Per docs/07-RETRY-IDEMPOTENCY.md: the same notification must not be sent twice.

Strategy:
- Redis fast path: `idem:{key}` -> notification_id (TTL bounded).
- PostgreSQL durable path: `idempotency_keys` table (source of truth).
- Server-derived fallback key: sha256(channel|recipient|message|reference).

The worker re-checks the durable notification state before sending, so queue
redelivery / crashes never double-send.
"""
import hashlib
import logging
import uuid
from typing import Dict, Optional, Tuple

from app.config import get_settings

logger = logging.getLogger("idempotency")


def derive_key(channel: str, recipient: str, message: str, reference: Optional[str] = None) -> str:
    """Server-derived idempotency key when the client sends no Idempotency-Key."""
    raw = f"{channel}|{recipient}|{hashlib.sha256(message.encode()).hexdigest()}|{reference or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_client_key(key: str) -> str:
    """Normalize a client-supplied key (trim, lowercase; reject control chars)."""
    key = key.strip().lower()
    if any(ord(c) < 32 for c in key):
        raise ValueError("Idempotency-Key contains control characters")
    if len(key) > 128:
        raise ValueError("Idempotency-Key too long (max 128 chars)")
    return key


def _redis():
    import redis

    from app.config import get_settings

    settings = get_settings()
    kwargs: Dict = {"decode_responses": True}
    if settings.REDIS_PASSWORD:
        kwargs["password"] = settings.REDIS_PASSWORD
    return redis.Redis.from_url(settings.REDIS_URL, **kwargs)


def check_redis(key: str) -> Optional[str]:
    """Return the stored notification_id for a key, or None (fast path)."""
    try:
        r = _redis()
        return r.get(f"idem:{key}")
    except Exception as exc:  # noqa: BLE001 - fail open to DB path
        logger.warning("idempotency redis check failed: %s", exc)
        return None


def store_redis(key: str, notification_id: str) -> None:
    try:
        r = _redis()
        r.set(f"idem:{key}", notification_id, ex=get_settings().IDEMPOTENCY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("idempotency redis store failed: %s", exc)


def check_durable(key: str, payload_hash: str) -> Tuple[Optional[str], bool]:
    """
    Check/insert idempotency key in PostgreSQL (durable).

    Returns (notification_id, is_new). If the key already exists (with any
    payload), returns the stored notification id and is_new=False so the
    caller can replay the original result. Payload-hash conflicts are left to
    the caller to enforce (409) when desired.
    """
    from app.storage import get_storage

    storage = get_storage()
    existing = storage.find_idempotency_key_row(key)
    if existing is not None:
        return existing.get("notification_id"), False
    notification_id = str(uuid.uuid4())
    stored = storage.store_idempotency_key(key, notification_id, payload_hash)
    if not stored:
        # concurrent insert lost; re-read
        again = storage.find_idempotency_key_row(key)
        return (again.get("notification_id") if again else None), False
    return notification_id, True


def payload_hash(payload: Dict) -> str:
    """Stable hash of the send request payload for conflict detection."""
    import json

    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()