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
import json
import logging
import uuid
from typing import Dict, Optional, Tuple

from app.config import get_settings

logger = logging.getLogger("idempotency")


def content_fingerprint(
    user_id: str,
    channel: str,
    recipient: str,
    message: str,
    template_name: Optional[str] = None,
    template_params: Optional[Dict[str, str]] = None,
) -> str:
    """Stable content fingerprint used for window-based duplicate detection.

    Identifies "the same notification" by user + channel + recipient +
    message/template content (per the duplicate-detection rules). For template
    sends the fingerprint covers the template name + parameters; for free-text
    sends it covers the message body. The message/template content is hashed,
    never stored or logged in plain form.
    """
    if template_name:
        params = ""
        if template_params:
            params = json.dumps(template_params, sort_keys=True, default=str)
        raw = f"{user_id or ''}|{channel}|{recipient}|template:{template_name}|{params}"
    else:
        raw = f"{user_id or ''}|{channel}|{recipient}|{message}"
    return hashlib.sha256(raw.encode()).hexdigest()


def derive_key(channel: str, recipient: str, message: str, reference: Optional[str] = None,
               user_id: Optional[str] = None) -> str:
    """Server-derived idempotency key when the client sends no Idempotency-Key.

    Includes the user so identical content sent by different users is never
    treated as the same notification.
    """
    raw = f"{user_id or ''}|{channel}|{recipient}|{hashlib.sha256(message.encode()).hexdigest()}|{reference or ''}"
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
        notification_id = r.get(f"idem:{key}")
        logger.debug("idempotency redis lookup hit=%s", notification_id is not None)
        return notification_id
    except Exception as exc:  # noqa: BLE001 - fail open to DB path
        logger.warning("idempotency redis check failed: %s", exc)
        return None


def store_redis(key: str, notification_id: str, ex: Optional[int] = None) -> None:
    try:
        r = _redis()
        r.set(f"idem:{key}", notification_id, ex=ex or get_settings().IDEMPOTENCY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("idempotency redis store failed: %s", exc)


def claim_idempotency_key(key: str, notification_id: str, payload_hash: str,
                          ttl_seconds: Optional[int] = None) -> bool:
    """Atomically claim an idempotency key. Returns True if this caller won
    the claim (first to insert with this key). False if the key already exists
    (another caller or a previous request claimed it). The DB unique constraint
    `idempotency_keys.key` is the concurrency mutex.

    Transient SQLite lock errors are retried a few times so the winner's commit
    settles; only a genuine unique-constraint violation returns False.

    Must be called with the `notification_id` (message_id) that will be used
    for the notification, so a replay can return the correct result.
    """
    import time as _time

    from app.storage import get_storage

    storage = get_storage()
    logger.debug("idempotency durable claim started notification_id=%s", notification_id)
    for _ in range(8):
        try:
            return storage.store_idempotency_key(key, notification_id, payload_hash, ttl_seconds=ttl_seconds)
        except Exception:  # noqa: BLE001 - lock contention; retry briefly
            _time.sleep(0.01)
    # Last attempt - propagate a genuine error.
    return storage.store_idempotency_key(key, notification_id, payload_hash, ttl_seconds=ttl_seconds)


def check_durable(key: str, payload_hash: str,
                  ttl_seconds: Optional[int] = None) -> Tuple[Optional[str], bool]:
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
    stored = storage.store_idempotency_key(key, notification_id, payload_hash, ttl_seconds=ttl_seconds)
    if not stored:
        again = storage.find_idempotency_key_row(key)
        return (again.get("notification_id") if again else None), False
    return notification_id, True


def payload_hash(payload: Dict) -> str:
    """Stable hash of the send request payload for conflict detection."""
    import json

    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
