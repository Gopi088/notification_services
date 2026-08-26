"""
In-process (memory) queue backend for local / single-instance development.

When QUEUE_BACKEND=memory, notifications are delivered by asyncio tasks inside
the API process instead of Redis Streams + separate workers. This is safe for
single-instance dev but NOT for multi-container HA (a process restart loses
pending in-memory jobs). Notifications are always persisted as `queued` first,
and a reconciliation pass re-enqueues orphaned `queued` rows on startup.

Per docs/QUEUE_ARCHITECTURE.md, MODE B.
"""
import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("memory_queue")


class MemoryQueue:
    """A tiny in-process queue. Each channel has an asyncio.Queue of payload dicts."""

    def __init__(self, worker_callback=None):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._worker_callback = worker_callback
        self._started = False

    def _q(self, channel: str) -> asyncio.Queue:
        if channel not in self._queues:
            self._queues[channel] = asyncio.Queue()
        return self._queues[channel]

    def start(self, worker_callback=None) -> None:
        """Start one consumer task per channel."""
        if worker_callback:
            self._worker_callback = worker_callback
        if self._started:
            return
        self._started = True
        for channel in ("sms", "whatsapp", "email"):
            self._tasks[channel] = asyncio.create_task(self._run(channel))
        logger.info("memory queue started (channels: sms, whatsapp, email)")

    async def _run(self, channel: str) -> None:
        q = self._q(channel)
        while True:
            try:
                payload = await q.get()
                try:
                    if self._worker_callback:
                        await self._worker_callback(channel, payload)
                except Exception as exc:  # noqa: BLE001 - never kill the consumer
                    logger.error("memory queue worker error channel=%s: %s", channel, exc)
                finally:
                    q.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("memory queue channel loop error: %s", exc)

    def publish(self, channel: str, notification_id: str, group_id: Optional[str],
                recipient: str, attempt: int = 1, **extra) -> str:
        """Enqueue a job. Returns a fake 'entry id'."""
        payload: Dict[str, Any] = {
            "event_id": f"EVT_{uuid.uuid4().hex[:12]}",
            "notification_id": notification_id,
            "group_id": group_id,
            "channel": channel,
            "recipient": recipient,
            "attempt": attempt,
        }
        payload.update(extra)
        self._q(channel).put_nowait(payload)
        logger.info("memory queue publish channel=%s notification_id=%s",
                    channel, notification_id)
        return f"mem-{uuid.uuid4().hex[:12]}"

    def queue_length(self, channel: str) -> int:
        return self._q(channel).qsize() if channel in self._queues else 0

    def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._started = False
        logger.info("memory queue stopped")


_queue: Optional[MemoryQueue] = None


def get_memory_queue() -> MemoryQueue:
    global _queue
    if _queue is None:
        _queue = MemoryQueue()
    return _queue


def reset_memory_queue() -> None:
    global _queue
    if _queue is not None:
        _queue.stop()
        _queue = None
