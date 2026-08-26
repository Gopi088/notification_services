"""
Async worker pool.

Each worker runs in a loop:
1. Pick a message from the queue
2. Call the appropriate provider
3. Update database status
4. Repeat

Workers run as asyncio tasks (cooperative multitasking).
Provider calls use run_in_executor to avoid blocking the event loop.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Dict

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

_workers: list[asyncio.Task] = []
_worker_count: int = 0


async def _send_with_retry(item: QueueItem) -> None:
    """Send one message through its provider with retry logic."""
    settings = get_settings()
    max_attempts = settings.RETRY_MAX_ATTEMPTS
    backoff_base = settings.RETRY_BACKOFF_BASE_SECONDS
    backoff_max = settings.RETRY_BACKOFF_MAX_SECONDS
    channel = Channel(item.channel)
    msg_id = item.message_id

    base_extra = {"message_id": msg_id, "channel": item.channel}

    def _do_send():
        provider = get_provider(channel)
        logger.debug(
            "Resolving provider: channel=%s provider=%s mock_mode=%s",
            item.channel, provider.name, settings.MOCK_MODE,
            extra=base_extra,
        )
        if not settings.MOCK_MODE and item.template_name:
            return provider.send_with_template(
                item.contact,
                item.message,
                template_name=item.template_name,
                template_language=item.template_language,
                template_params=item.template_params,
            )
        return provider.send(item.contact, item.message)

    loop = asyncio.get_event_loop()

    for attempt in range(1, max_attempts + 1):
        provider_name = get_provider(channel).name
        attempt_extra = {**base_extra, "attempt": attempt, "max_attempts": max_attempts, "provider": provider_name}

        logger.info(
            "Calling provider: message_id=%s provider=%s attempt=%d/%d",
            msg_id, provider_name, attempt, max_attempts,
            extra=attempt_extra,
        )
        logger.debug(
            "Sending request to %s: to=%s message_length=%d",
            provider_name, item.contact[:4] + "***", len(item.message),
            extra=attempt_extra,
        )

        start = time.monotonic()
        try:
            result = await loop.run_in_executor(None, _do_send)
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)

            update_status(
                msg_id,
                status=result.status,
                provider=result.provider_name,
                provider_message_id=result.provider_message_id,
            )
            increment_attempt(msg_id)

            logger.info(
                "Provider accepted message: message_id=%s provider=%s provider_msg_id=%s status=%s attempt=%d duration=%.1fms",
                msg_id, result.provider_name, result.provider_message_id,
                result.status, attempt, elapsed_ms,
                extra={**base_extra, "provider": result.provider_name,
                       "provider_msg_id": result.provider_message_id,
                       "duration_ms": elapsed_ms, "attempt": attempt},
            )
            logger.debug(
                "Provider response details: provider=%s status=%s provider_msg_id=%s raw_duration=%.1fms",
                result.provider_name, result.status, result.provider_message_id, elapsed_ms,
                extra=base_extra,
            )

            # Simulate delivery in mock mode
            if settings.MOCK_MODE:
                async def _simulate():
                    await asyncio.sleep(1.5)
                    update_status(msg_id, status="delivered")
                    logger.info(
                        "Mock delivery confirmed: message_id=%s",
                        msg_id,
                        extra=base_extra,
                    )
                asyncio.create_task(_simulate())

            return

        except ProviderPermanentError as exc:
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            logger.error(
                "Permanent failure: message_id=%s provider=%s error=%s attempt=%d duration=%.1fms",
                msg_id, provider_name, exc, attempt, elapsed_ms,
                extra={**base_extra, "provider": provider_name,
                       "duration_ms": elapsed_ms, "attempt": attempt},
            )
            increment_attempt(msg_id)
            update_status(msg_id, status="failed", error=str(exc))
            message_queue.mark_failed()
            return

        except ProviderTransientError as exc:
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            logger.warning(
                "Transient error: message_id=%s provider=%s error=%s attempt=%d/%d duration=%.1fms",
                msg_id, provider_name, exc, attempt, max_attempts, elapsed_ms,
                extra={**base_extra, "provider": provider_name,
                       "duration_ms": elapsed_ms, "attempt": attempt,
                       "max_attempts": max_attempts},
            )
            increment_attempt(msg_id)
            if attempt == max_attempts:
                logger.error(
                    "Failed after %d attempts: message_id=%s provider=%s last_error=%s",
                    attempt, msg_id, provider_name, exc,
                    extra={**base_extra, "provider": provider_name,
                           "attempt": attempt, "max_attempts": max_attempts},
                )
                update_status(msg_id, status="failed",
                            error=f"Failed after {attempt} attempts: {exc}")
                message_queue.mark_failed()
                return
            delay = min(
                backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.25),
                backoff_max,
            )
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            set_retry_schedule(msg_id, next_retry)
            update_status(msg_id, status="retrying")
            logger.warning(
                "Scheduling retry: message_id=%s attempt=%d/%d delay=%.1fs next_retry=%s",
                msg_id, attempt, max_attempts, delay, next_retry,
                extra={**base_extra, "attempt": attempt,
                       "max_attempts": max_attempts, "duration_ms": elapsed_ms},
            )
            logger.debug(
                "Retry backoff details: base=%.1fs max=%.1fs jitter=%.3fs calculated=%.1fs",
                backoff_base, backoff_max, delay - (backoff_base * (2 ** (attempt - 1))),
                delay, extra=base_extra,
            )
            await asyncio.sleep(delay)

        except Exception as exc:
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            logger.exception(
                "Unexpected error: message_id=%s provider=%s attempt=%d duration=%.1fms",
                msg_id, provider_name, attempt, elapsed_ms,
                extra={**base_extra, "provider": provider_name,
                       "duration_ms": elapsed_ms, "attempt": attempt},
            )
            increment_attempt(msg_id)
            update_status(msg_id, status="failed", error=f"Unexpected error: {exc}")
            message_queue.mark_failed()
            return

    logger.error(
        "Exhausted all retries: message_id=%s attempts=%d",
        msg_id, max_attempts,
        extra={**base_extra, "max_attempts": max_attempts},
    )
    update_status(msg_id, status="failed", error=f"Failed after {max_attempts} attempts")
    message_queue.mark_failed()


async def worker(worker_id: int) -> None:
    """One worker loop -- picks messages from queue and sends them."""
    logger.info("Worker %d started", worker_id)
    while True:
        try:
            item = await message_queue.get()
            try:
                # Restore the request_id from the original HTTP request
                # so all logs in this worker carry the correct correlation ID
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
                await _send_with_retry(item)
            finally:
                # Clear request_id after processing to avoid leaking to next item
                request_id_var.set("")
                message_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Worker %d stopped", worker_id)
            break
        except Exception:
            logger.critical(
                "Worker %d crashed — restarting in 2s. This may indicate a system problem.",
                worker_id,
            )
            await asyncio.sleep(2)
            # Worker auto-restarts by continuing the while loop


def start_workers(count: int) -> list[asyncio.Task]:
    """Start N worker tasks. Call this on app startup."""
    global _workers
    _workers = [asyncio.create_task(worker(i)) for i in range(count)]
    logger.info("Started %d workers", count)
    return _workers


async def stop_workers() -> None:
    """Stop all workers gracefully. Call this on app shutdown."""
    for w in _workers:
        w.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    logger.info("All workers stopped")
