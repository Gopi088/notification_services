"""
Message queue: Redis Streams producer/consumer.

Per docs/04-MESSAGE-QUEUE.md. Uses Redis Streams with consumer groups:

    notifications:<channel>   main per-channel streams
    notifications:retry       delayed retries (scheduled_at)
    notifications:dlq         dead-letter queue

Redis is a queue transport only; PostgreSQL remains the durable source of truth.
"""
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from app.config import get_settings
from app.providers.base import sanitize_provider_error

logger = logging.getLogger("queue")


class QueueError(Exception):
    """Raised when the queue cannot be published to or consumed from."""


def _client():
    import redis

    settings = get_settings()
    kwargs: Dict[str, Any] = {"decode_responses": True}
    if settings.REDIS_PASSWORD:
        kwargs["password"] = settings.REDIS_PASSWORD
    return redis.Redis.from_url(settings.REDIS_URL, **kwargs)


def stream_name(channel: str) -> str:
    return f"{get_settings().QUEUE_STREAM_PREFIX}:{channel}"


def retry_stream_name() -> str:
    return f"{get_settings().QUEUE_STREAM_PREFIX}:retry"


def dlq_stream_name() -> str:
    return f"{get_settings().QUEUE_STREAM_PREFIX}:dlq"


def _message(channel: str, notification_id: str, group_id: Optional[str],
             recipient: str, attempt: int, **extra) -> str:
    payload: Dict[str, Any] = {
        "event_id": f"EVT_{uuid.uuid4().hex[:12]}",
        "notification_id": notification_id,
        "group_id": group_id,
        "channel": channel,
        "recipient": recipient,
        "attempt": attempt,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }
    payload.update(extra)
    return json.dumps(payload)


def publish(channel: str, notification_id: str, group_id: Optional[str],
            recipient: str, attempt: int = 1, **extra) -> str:
    """Publish a notification to its channel stream. Returns the stream entry id."""
    settings = get_settings()
    body = _message(channel, notification_id, group_id, recipient, attempt, **extra)
    if len(body.encode()) > settings.QUEUE_MESSAGE_MAX_BYTES:
        raise QueueError("queue message exceeds QUEUE_MESSAGE_MAX_BYTES")
    try:
        r = _client()
        entry_id = r.xadd(stream_name(channel), {"payload": body}, maxlen=100000, approximate=True)
        logger.info(
            "queue publish channel=%s notification_id=%s group_id=%s entry=%s",
            channel, notification_id, group_id, entry_id,
        )
        logger.debug("queue message published notification_id=%s channel=%s attempt=%d",
                     notification_id, channel, attempt)
        return entry_id
    except Exception as exc:  # noqa: BLE001
        safe_error = sanitize_provider_error(exc)
        logger.error("queue publish failed channel=%s notification_id=%s: %s",
                     channel, notification_id, safe_error)
        raise QueueError(f"queue publish failed: {safe_error}") from exc


def publish_retry(channel: str, notification_id: str, group_id: Optional[str],
                  recipient: str, attempt: int, scheduled_at: float, **extra) -> str:
    """Publish a delayed retry to the retry stream (scheduled_at = epoch seconds)."""
    body = json.loads(_message(channel, notification_id, group_id, recipient, attempt, **extra))
    body["scheduled_at"] = scheduled_at
    try:
        r = _client()
        entry_id = r.xadd(retry_stream_name(), {"payload": json.dumps(body)},
                          maxlen=100000, approximate=True)
        logger.warning(
            "queue retry scheduled channel=%s notification_id=%s attempt=%d scheduled_at=%.2f",
            channel, notification_id, attempt, scheduled_at,
        )
        logger.debug("retry queue message published notification_id=%s channel=%s attempt=%d",
                     notification_id, channel, attempt)
        return entry_id
    except Exception as exc:  # noqa: BLE001
        safe_error = sanitize_provider_error(exc)
        logger.error("queue retry publish failed notification_id=%s: %s", notification_id, safe_error)
        raise QueueError(f"queue retry publish failed: {safe_error}") from exc


def publish_dlq(channel: str, notification_id: str, group_id: Optional[str],
                recipient: str, attempt: int, reason: str, error_code: Optional[str] = None,
                error_message: Optional[str] = None) -> str:
    body = json.loads(_message(channel, notification_id, group_id, recipient, attempt))
    body["dlq_reason"] = reason
    body["error_code"] = error_code
    body["error_message"] = error_message
    try:
        r = _client()
        entry_id = r.xadd(dlq_stream_name(), {"payload": json.dumps(body)},
                          maxlen=100000, approximate=True)
        logger.error(
            "queue dead-letter notification_id=%s channel=%s reason=%s",
            notification_id, channel, reason,
        )
        return entry_id
    except Exception as exc:  # noqa: BLE001
        safe_error = sanitize_provider_error(exc)
        logger.error("queue dlq publish failed notification_id=%s: %s", notification_id, safe_error)
        raise QueueError(f"queue dlq publish failed: {safe_error}") from exc


def ensure_group(channel: str) -> None:
    """Create the consumer group (idempotent)."""
    r = _client()
    stream = stream_name(channel)
    group = get_settings().QUEUE_CONSUMER_GROUP
    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("queue consumer group created stream=%s group=%s", stream, group)
    except Exception:
        # group already exists (Redis error BUSYGROUP)
        pass


def consume(channel: str, worker_id: str, count: int = 1, block_ms: Optional[int] = None):
    """Blocking read from the channel stream consumer group."""
    ensure_group(channel)
    settings = get_settings()
    block = block_ms if block_ms is not None else settings.QUEUE_BLOCK_MS
    try:
        r = _client()
        return r.xreadgroup(
            groupname=settings.QUEUE_CONSUMER_GROUP,
            consumername=worker_id,
            streams={stream_name(channel): ">"},
            count=count,
            block=block,
        )
    except Exception as exc:  # noqa: BLE001
        # A blocking read that times out is normal between polls; return empty
        # so the worker keeps looping. Other errors still surface as QueueError.
        if "timeout" in str(exc).lower():
            logger.debug("queue consume blocked (no messages) channel=%s", channel)
            return []
        safe_error = sanitize_provider_error(exc)
        logger.error("queue consume failed channel=%s: %s", channel, safe_error)
        raise QueueError(f"queue consume failed: {safe_error}") from exc


def ack(channel: str, entry_id: str) -> None:
    r = _client()
    settings = get_settings()
    r.xack(stream_name(channel), settings.QUEUE_CONSUMER_GROUP, entry_id)


def claim_pending(channel: str, worker_id: str, min_idle_ms: Optional[int] = None) -> list:
    """Reclaim messages from dead workers (XAUTOCLAIM)."""
    ensure_group(channel)
    r = _client()
    settings = get_settings()
    idle = min_idle_ms if min_idle_ms is not None else settings.QUEUE_VISIBILITY_TIMEOUT_MS
    try:
        _next_id, claimed, _deleted = r.xautoclaim(
            stream_name(channel), settings.QUEUE_CONSUMER_GROUP, worker_id,
            min_idle_time=idle, start_id="0-0", count=20,
        )
        return claimed
    except Exception as exc:  # noqa: BLE001
        safe_error = sanitize_provider_error(exc)
        logger.error("queue claim_pending failed channel=%s: %s", channel, safe_error)
        raise QueueError(f"queue claim failed: {safe_error}") from exc


def queue_length(channel: str) -> int:
    try:
        r = _client()
        return int(r.xlen(stream_name(channel)))
    except Exception:
        return 0
