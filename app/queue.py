"""
Thread-safe message queue.

Requests come in -> stored in queue -> worker threads pick up and send.
This decouples the API layer from the provider layer.

Uses queue.Queue (OS-level thread safe) instead of asyncio.Queue.
"""
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("queue")


@dataclass
class QueueItem:
    """One message waiting to be sent."""
    message_id: str
    channel: str
    contact: str
    message: str
    reference: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_params: Optional[Dict[str, str]] = None
    request_id: Optional[str] = None


class MessageQueue:
    """Thread-safe queue that holds messages waiting to be sent."""

    def __init__(self, max_size: int = 1000):
        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=max_size)
        self._lock = threading.Lock()
        self._processing: int = 0
        self._total_processed: int = 0
        self._total_failed: int = 0

    def put(self, item: QueueItem) -> None:
        """Add a message to the queue (non-blocking)."""
        self._queue.put_nowait(item)
        logger.info("Queued message %s for %s (queue size: %d)", item.message_id, item.channel, self._queue.qsize())

    def get(self, timeout: Optional[float] = None) -> QueueItem:
        """Get next message from queue (blocks until available or timeout).

        Args:
            timeout: Max seconds to wait. None = block forever.

        Raises:
            queue.Empty: If timeout expires with no item available.
        """
        item = self._queue.get(timeout=timeout)
        with self._lock:
            self._processing += 1
        logger.info("Picked up message %s (remaining: %d, processing: %d)",
                     item.message_id, self._queue.qsize(), self._processing)
        return item

    def task_done(self) -> None:
        """Mark a task as complete."""
        self._queue.task_done()
        with self._lock:
            self._processing -= 1
            self._total_processed += 1

    def mark_failed(self) -> None:
        """Mark a task as failed."""
        with self._lock:
            self._total_failed += 1

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def processing(self) -> int:
        with self._lock:
            return self._processing

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "pending": self._queue.qsize(),
                "processing": self._processing,
                "total_processed": self._total_processed,
                "total_failed": self._total_failed,
            }


# Global singleton
message_queue = MessageQueue()
