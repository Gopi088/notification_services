"""
Notification worker.

Consumes from Redis Streams consumer groups and delivers notifications through
the provider layer. Per docs/05-WORKER-DESIGN.md.

Lifecycle:
  consume -> load notification -> idempotency check -> status=processing ->
  select provider -> send -> persist result -> ack (or retry/DLQ)

Graceful shutdown on SIGTERM: stop reading, finish in-flight, exit.
"""
import json
import logging
import signal
import threading
import time
import uuid
from typing import Dict, Optional

from app.config import get_settings
from app.audit import record_audit
from app.providers.base import ProviderConfigError, ProviderError, sanitize_provider_error
from app.providers.factory import get_provider
from app.retry import backoff_delay_ms, is_retryable_error
from app.storage import (
    PROCESSING,
    SUBMITTED,
    RETRYING,
    DEAD_LETTERED,
    FAILED,
    get_storage,
)
from app import queue as q

logger = logging.getLogger("worker")


def process_message(channel: str, payload: Dict) -> bool:
    """
    Process one queue message. Returns True if it should be ACKed.

    Loads the notification from PostgreSQL (source of truth), guards state,
    sends via the provider, persists the result, and routes retries/DLQ.
    """
    storage = get_storage()
    notification_id = payload.get("notification_id")
    attempt = int(payload.get("attempt", 1))
    logger.debug("worker received notification_id=%s channel=%s attempt=%d", notification_id, channel, attempt)

    notification = storage.get_notification(notification_id)
    if notification is None:
        logger.error("worker notification not found id=%s group_id=%s (DLQ)",
                     notification_id, payload.get("group_id"))
        q.publish_dlq(channel, notification_id, payload.get("group_id"),
                      payload.get("recipient", ""), attempt,
                      reason="notification_not_found", error_code="NOT_FOUND")
        return True  # ack: nothing to reprocess

    # Idempotency guard: if already terminal/submitted by a prior redelivery, skip.
    if notification["status"] in (SUBMITTED, "delivered"):
        logger.warning("worker duplicate delivery skipped id=%s group_id=%s status=%s",
                       notification_id, notification.get("group_id"), notification["status"])
        record_audit(
            user_id=notification.get("created_by"),
            action="duplicate_notification_attempted", notification_id=notification_id,
            channel=channel, status=notification["status"],
        )
        return True

    # Acquire the DB row atomically.  A Redis delivery can be duplicated (for
    # example after a consumer crash); the winner alone is allowed to send.
    updated = storage.claim_for_processing(
        notification_id, actor="worker", from_status=notification["status"],
    )
    if updated is None:
        logger.warning("worker skipping id=%s group_id=%s status was claimed elsewhere",
                       notification_id, notification.get("group_id"))
        return True
    logger.debug("worker processing notification_id=%s channel=%s status=processing", notification_id, channel)
    record_audit(
        user_id=notification.get("created_by"),
        action="notification_processing", notification_id=notification_id,
        channel=channel, status="processing",
    )

    # Record retry_attempted when this is a retry (attempt > 1), so retry
    # history is visible end to end.
    if attempt > 1:
        record_audit(
            user_id=notification.get("created_by"),
            action="retry_attempted", notification_id=notification_id,
            channel=channel, status="processing",
            metadata={"attempt": attempt},
        )

    provider = None
    started = time.monotonic()
    try:
        provider = get_provider(__import__("app.schemas", fromlist=["Channel"]).Channel(channel))
        logger.info("worker provider selected notification_id=%s group_id=%s provider=%s",
                    notification_id, notification.get("group_id"), provider.name)

        # Provider rate-limit guard (best-effort, fail-open).
        from app import ratelimit
        rl = ratelimit.check_provider(provider.name, 500)
        if not rl.allowed:
            logger.warning("worker provider throttled notification_id=%s provider=%s",
                           notification_id, provider.name)
            raise ProviderError(
                "provider rate limit exceeded",
                retryable=True, error_code="429",
            )

        message = notification.get("message") or ""
        recipient = notification.get("recipient") or payload.get("recipient", "")
        template_name = notification.get("template_name")
        result = None
        if template_name:
            import json as _json
            params = {}
            tp = notification.get("template_params")
            if tp:
                try:
                    params = _json.loads(tp) if isinstance(tp, str) else dict(tp)
                except Exception:
                    params = {}
            result = provider.send_with_template(
                recipient, message,
                template_name=template_name,
                template_language=notification.get("template_language"),
                template_params=params,
            )
        else:
            logger.debug("worker provider send starting notification_id=%s channel=%s provider=%s",
                         notification_id, channel, provider.name)
            result = provider.send(recipient, message)

        duration_ms = int((time.monotonic() - started) * 1000)
        storage.transition(
            notification_id, SUBMITTED, actor="worker",
            provider=result.provider_name,
            provider_message_id=result.provider_message_id,
        )
        storage.add_attempt(
            notification_id, attempt, SUBMITTED,
            provider=result.provider_name,
            provider_message_id=result.provider_message_id,
            duration_ms=duration_ms,
        )
        logger.info(
            "worker sent notification_id=%s group_id=%s provider=%s provider_message_id=%s attempt=%d latency_ms=%d",
            notification_id, notification.get("group_id"), result.provider_name,
            result.provider_message_id, attempt, duration_ms,
        )
        logger.debug("worker response ready notification_id=%s channel=%s status=submitted",
                     notification_id, channel)
        record_audit(
            user_id=notification.get("created_by"),
            action="notification_sent", notification_id=notification_id,
            channel=channel, status="submitted", provider=result.provider_name,
        )
        return True

    except (ProviderConfigError, ProviderError) as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        retryable = is_retryable_error(exc)
        error_code = getattr(exc, "error_code", None)
        error_msg = sanitize_provider_error(exc)

        storage.add_attempt(
            notification_id, attempt, FAILED,
            provider=provider.name if provider else None,
            error_code=error_code, error_message=error_msg,
            retryable=retryable, duration_ms=duration_ms,
        )
        logger.error(
            "worker provider failed notification_id=%s group_id=%s attempt=%d retryable=%s error_code=%s error=%s",
            notification_id, notification.get("group_id"), attempt, retryable, error_code, error_msg,
        )

        if retryable and attempt < notification.get("max_attempts", get_settings().MAX_ATTEMPTS):
            storage.transition(notification_id, RETRYING, actor="worker", error=error_msg)
            delay = backoff_delay_ms(attempt)
            scheduled = time.time() + delay / 1000.0
            q.publish_retry(channel, notification_id, notification.get("group_id"),
                            notification.get("recipient"), attempt + 1, scheduled)
            logger.warning(
                "worker retry scheduled notification_id=%s group_id=%s attempt=%d delay_ms=%d",
                notification_id, notification.get("group_id"), attempt + 1, delay,
            )
            logger.debug("worker retry queued notification_id=%s channel=%s status=retrying attempt=%d",
                         notification_id, channel, attempt + 1)
            record_audit(
                user_id=notification.get("created_by"),
                action="retry_scheduled", notification_id=notification_id,
                channel=channel, status=RETRYING, result="failure",
                failure_reason=error_msg,
                metadata={"attempt": attempt, "next_attempt": attempt + 1, "delay_ms": delay},
            )
        else:
            storage.transition(
                notification_id, DEAD_LETTERED if retryable else FAILED,
                actor="worker", error=error_msg,
            )
            q.publish_dlq(channel, notification_id, notification.get("group_id"),
                          notification.get("recipient"), attempt,
                          reason="max_attempts" if retryable else "non_retryable",
                          error_code=error_code, error_message=error_msg)
            logger.error(
                "worker dead-lettered notification_id=%s group_id=%s reason=%s",
                notification_id, notification.get("group_id"),
                "max_attempts" if retryable else "non_retryable",
            )
            record_audit(
                user_id=notification.get("created_by"),
                action="notification_failed", notification_id=notification_id,
                channel=channel, status=DEAD_LETTERED if retryable else FAILED,
                result="failure", failure_reason=error_msg,
                metadata={"dead_lettered": True},
            )
        return True

    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        error_msg = "Unexpected provider failure."
        storage.add_attempt(
            notification_id, attempt, FAILED,
            provider=provider.name if provider else None,
            error_code="UNEXPECTED", error_message=error_msg,
            retryable=True, duration_ms=duration_ms,
        )
        # An unexpected SDK/runtime failure is retried through the durable
        # retry stream rather than leaving an acknowledged row in processing.
        storage.transition(notification_id, RETRYING, actor="worker", error=error_msg)
        try:
            delay = backoff_delay_ms(attempt)
            q.publish_retry(channel, notification_id, notification.get("group_id"),
                            notification.get("recipient"), attempt + 1,
                            time.time() + delay / 1000.0)
        except Exception:  # noqa: BLE001
            # Leave the stream entry pending so Redis can reclaim it; never
            # acknowledge a notification whose recovery could not be queued.
            logger.exception("worker could not persist retry notification_id=%s", notification_id)
            return False
        logger.exception("worker unexpected error notification_id=%s", notification_id)
        return True


def _process_entries(channel: str, entries) -> bool:
    """Process Redis stream entries and ACK only durable outcomes."""
    if not entries:
        return False
    for stream_key, messages in entries:
        for entry_id, fields in messages:
            try:
                payload = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError:
                logger.error("worker malformed queue message entry=%s (DLQ)", entry_id)
                q.publish_dlq(channel, "unknown", None, "", 1,
                              reason="malformed_message", error_code="BAD_JSON")
                q.ack(channel, entry_id)
                continue
            try:
                ack_it = process_message(channel, payload)
            except Exception as exc:  # noqa: BLE001
                logger.exception("worker process_message crashed notification_id=%s", payload.get("notification_id"))
                ack_it = True
            if ack_it:
                q.ack(channel, entry_id)
    return True


def _run_once(channel: str, worker_id: str) -> bool:
    """Recover abandoned work, then consume one new batch."""
    try:
        claimed = q.claim_pending(channel, worker_id)
        if claimed:
            logger.info("worker reclaimed pending entries channel=%s count=%d", channel, len(claimed))
            return _process_entries(channel, [(q.stream_name(channel), claimed)])
        entries = q.consume(channel, worker_id, count=1)
    except q.QueueError as exc:
        logger.error("worker consume failed channel=%s: %s", channel, exc)
        time.sleep(1)
        return False
    return _process_entries(channel, entries)


def run_worker(channel: str, worker_id: Optional[str] = None) -> None:
    """Main worker loop for one channel. Blocks until SIGTERM."""
    settings = get_settings()
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    logger.info("worker started channel=%s worker_id=%s", channel, worker_id)
    stop = threading.Event()

    def _handle(signum, frame):  # noqa: ANN001
        logger.info("worker received SIGTERM channel=%s starting graceful shutdown", channel)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    concurrency_key = f"WORKER_CONCURRENCY_{channel.upper()}"
    concurrency = int(getattr(settings, concurrency_key, settings.WORKER_CONCURRENCY))
    sem = threading.Semaphore(concurrency)

    def _thread_loop():
        while not stop.is_set():
            if sem.acquire(blocking=False):
                try:
                    _run_once(channel, worker_id)
                finally:
                    sem.release()
            else:
                time.sleep(0.05)

    threads = [threading.Thread(target=_thread_loop, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    stop.wait()
    logger.info("worker stopping channel=%s (finishing in-flight)", channel)
    # wait for in-flight threads up to grace
    grace = settings.WORKER_GRACE_SECONDS
    deadline = time.time() + grace
    while time.time() < deadline and any(t.is_alive() for t in threads):
        time.sleep(0.1)
    logger.info("worker stopped channel=%s", channel)


def process_retry_stream() -> int:
    """
    Move due retries from the retry stream back to their channel stream.

    Called periodically by a retry worker or reconciliation job.
    Returns the number of retries requeued.
    """
    storage = get_storage()
    now = time.time()
    requeued = 0
    r = q._client()
    try:
        entries = r.xread({q.retry_stream_name(): "0-0"}, count=50, block=100)
    except Exception as exc:  # noqa: BLE001
        logger.error("retry stream read failed: %s", exc)
        return 0
    for _stream, messages in entries:
        for entry_id, fields in messages:
            try:
                payload = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError:
                r.xdel(q.retry_stream_name(), entry_id)
                continue
            scheduled = float(payload.get("scheduled_at", 0) or 0)
            if scheduled > now:
                continue
            channel = payload.get("channel", "sms")
            q.publish(channel, payload.get("notification_id"), payload.get("group_id"),
                      payload.get("recipient", ""), int(payload.get("attempt", 1)))
            r.xdel(q.retry_stream_name(), entry_id)
            requeued += 1
            logger.info("retry requeued notification_id=%s channel=%s attempt=%d",
                        payload.get("notification_id"), channel, int(payload.get("attempt", 1)))
    return requeued


def run_retry_worker() -> None:
    """Background loop that moves due retries back to channel streams."""
    logger.info("retry worker started")
    while True:
        try:
            process_retry_stream()
        except Exception as exc:  # noqa: BLE001
            logger.error("retry worker error: %s", exc)
        time.sleep(2)
