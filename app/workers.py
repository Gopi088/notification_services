"""
Thread-based worker pool with tenacity retry.

Each worker runs in a daemon thread:
1. Pick a message from the thread-safe queue
2. Call the appropriate provider (with retry via tenacity)
3. Update database status
4. Repeat

Workers are OS threads managed by WorkerManager.
Provider calls run in the worker thread directly (no event loop to block).
"""
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from app.config import get_settings
from app.database import (
    increment_attempt,
    set_retry_schedule,
    update_status,
)
from app.middleware import request_id_var
from app.providers.base import (
    ProviderPermanentError,
    ProviderTransientError,
)
from app.providers.factory import get_provider
from app.queue import QueueItem, message_queue
from app.schemas import Channel

logger = logging.getLogger("workers")


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    """Tenacity callback: log each retry attempt."""
    if retry_state.attempt_number <= 1:
        return
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    item = retry_state.kwargs.get("item")
    msg_id = item.message_id if item else "unknown"
    logger.warning(
        "Retry attempt %d for message_id=%s: %s",
        retry_state.attempt_number, msg_id, exc,
        extra={"message_id": msg_id, "attempt": retry_state.attempt_number},
    )


def _build_retry_decorator(item: QueueItem):
    """Build a tenacity retry decorator configured from app settings."""
    settings = get_settings()
    max_attempts = settings.RETRY_MAX_ATTEMPTS
    backoff_base = settings.RETRY_BACKOFF_BASE_SECONDS
    backoff_max = settings.RETRY_BACKOFF_MAX_SECONDS

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max) + wait_random(min=0, max=0.5),
        retry=retry_if_exception_type(ProviderTransientError),
        before=_log_retry_attempt,
        reraise=True,
    )


def _send_with_retry(item: QueueItem) -> None:
    """Send one message through its provider with tenacity retry logic.

    - ProviderPermanentError: immediately marks message as failed (no retry).
    - ProviderTransientError: retried with exponential backoff + jitter.
    - Unexpected exceptions: immediately marks message as failed (no retry).
    """
    settings = get_settings()
    channel = Channel(item.channel)
    msg_id = item.message_id
    base_extra = {"message_id": msg_id, "channel": item.channel}

    def _do_send():
        provider = get_provider(channel)
        if not settings.MOCK_MODE and item.template_name:
            return provider.send_with_template(
                item.contact,
                item.message,
                template_name=item.template_name,
                template_language=item.template_language,
                template_params=item.template_params,
            )
        return provider.send(item.contact, item.message)

    @_build_retry_decorator(item=item)
    def _attempt_send():
        provider_name = get_provider(channel).name
        attempt = _attempt_send.retry.statistics.get("attempt_number", 1)
        attempt_extra = {**base_extra, "attempt": attempt, "provider": provider_name}

        logger.info(
            "Calling provider: message_id=%s provider=%s attempt=%d",
            msg_id, provider_name, attempt,
            extra=attempt_extra,
        )
        logger.debug(
            "Sending request to %s: to=%s message_length=%d",
            provider_name, item.contact[:4] + "***", len(item.message),
            extra=attempt_extra,
        )

        start = time.monotonic()
        result = _do_send()
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        update_status(
            msg_id,
            status=result.status,
            provider=result.provider_name,
            provider_message_id=result.provider_message_id,
        )
        increment_attempt(msg_id)

        logger.info(
            "Provider accepted: message_id=%s provider=%s provider_msg_id=%s status=%s duration=%.1fms",
            msg_id, result.provider_name, result.provider_message_id,
            result.status, elapsed_ms,
            extra={**base_extra, "provider": result.provider_name,
                   "provider_msg_id": result.provider_message_id,
                   "duration_ms": elapsed_ms, "attempt": attempt},
        )

        # Simulate delivery in mock mode
        if settings.MOCK_MODE:
            def _simulate():
                time.sleep(1.5)
                update_status(msg_id, status="delivered")
                logger.info("Mock delivery confirmed: message_id=%s", msg_id, extra=base_extra)
            sim_thread = threading.Thread(target=_simulate, daemon=True)
            sim_thread.start()

        return result

    try:
        _attempt_send()
    except ProviderPermanentError as exc:
        logger.error(
            "Permanent failure: message_id=%s error=%s",
            msg_id, exc, extra=base_extra,
        )
        increment_attempt(msg_id)
        update_status(msg_id, status="failed", error=str(exc))
        message_queue.mark_failed()
    except ProviderTransientError as exc:
        # Tenacity exhausted all retries — final transient failure
        stats = _attempt_send.retry.statistics
        total_attempts = stats.get("attempt_number", settings.RETRY_MAX_ATTEMPTS)
        logger.error(
            "Failed after %d attempts: message_id=%s last_error=%s",
            total_attempts, msg_id, exc,
            extra={**base_extra, "attempt": total_attempts,
                   "max_attempts": settings.RETRY_MAX_ATTEMPTS},
        )
        increment_attempt(msg_id)
        update_status(msg_id, status="failed",
                      error=f"Failed after {total_attempts} attempts: {exc}")
        message_queue.mark_failed()
    except Exception as exc:
        logger.exception(
            "Unexpected error: message_id=%s error=%s",
            msg_id, exc, extra=base_extra,
        )
        increment_attempt(msg_id)
        update_status(msg_id, status="failed", error=f"Unexpected error: {exc}")
        message_queue.mark_failed()


def _worker_loop(worker_id: int, shutdown_event: threading.Event) -> None:
    """One worker loop -- picks messages from queue and sends them.

    Runs in a daemon thread. Blocks on queue.get() until an item is available
    or the shutdown event is set.
    """
    logger.info("Worker %d started (thread=%s)", worker_id, threading.current_thread().name)

    while not shutdown_event.is_set():
        try:
            item = message_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        try:
            if item.request_id:
                request_id_var.set(item.request_id)

            logger.info(
                "Worker %d picked up message: message_id=%s channel=%s",
                worker_id, item.message_id, item.channel,
                extra={"message_id": item.message_id, "channel": item.channel},
            )
            logger.debug(
                "Worker %d processing: contact=%s message_length=%d reference=%s",
                worker_id, item.contact[:4] + "***", len(item.message), item.reference,
                extra={"message_id": item.message_id, "channel": item.channel},
            )
            update_status(item.message_id, status="processing")
            _send_with_retry(item)
        except Exception:
            logger.critical(
                "Worker %d crashed -- restarting in 2s. This may indicate a system problem.",
                worker_id,
            )
            time.sleep(2)
        finally:
            request_id_var.set("")
            message_queue.task_done()

    logger.info("Worker %d stopped", worker_id)


class WorkerManager:
    """Manages a pool of daemon worker threads."""

    def __init__(self):
        self._threads: list[threading.Thread] = []
        self._shutdown_event = threading.Event()

    def start(self, count: int) -> None:
        """Start N worker threads. Call this on app startup."""
        self._shutdown_event.clear()
        self._threads = []
        for i in range(count):
            t = threading.Thread(
                target=_worker_loop,
                args=(i, self._shutdown_event),
                name=f"worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        logger.info("Started %d worker threads", count)

    def stop(self) -> None:
        """Stop all workers gracefully. Call this on app shutdown."""
        self._shutdown_event.set()
        for t in self._threads:
            t.join(timeout=5.0)
            if t.is_alive():
                logger.warning("Worker thread %s did not stop in time", t.name)
        self._threads.clear()
        logger.info("All worker threads stopped")


# Global singleton
worker_manager = WorkerManager()
