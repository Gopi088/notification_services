"""
Async message queue.

Requests come in → stored in queue → workers pick up and send.
This decouples the API layer from the provider layer.
"""
import asyncio
import logging
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
    """Async queue that holds messages waiting to be sent."""

    def __init__(self, max_size: int = 1000):
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=max_size)
        self._processing: int = 0
        self._total_processed: int = 0
        self._total_failed: int = 0

    async def put(self, item: QueueItem) -> None:
        """Add a message to the queue."""
        await self._queue.put(item)
        logger.info("Queued message %s for %s (queue size: %d)", item.message_id, item.channel, self._queue.qsize())

    async def get(self) -> QueueItem:
        """Get next message from queue (blocks until available)."""
        item = await self._queue.get()
        self._processing += 1
        logger.info("Picked up message %s (remaining: %d, processing: %d)",
                     item.message_id, self._queue.qsize(), self._processing)
        return item

    def task_done(self) -> None:
        """Mark a task as complete."""
        self._queue.task_done()
        self._processing -= 1
        self._total_processed += 1

    def mark_failed(self) -> None:
        """Mark a task as failed."""
        self._total_failed += 1

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def processing(self) -> int:
        return self._processing

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "pending": self._queue.qsize(),
            "processing": self._processing,
            "total_processed": self._total_processed,
            "total_failed": self._total_failed,
        }


# Global singleton
message_queue = MessageQueue()
