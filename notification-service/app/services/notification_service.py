"""
Core orchestration: takes a queued message, calls the right provider, and
keeps the SQLite record's status in sync.

Status lifecycle:
    queued  -> sent      (provider accepted the message)
    queued  -> failed    (validation/provider/config error)
    sent    -> delivered (mock mode simulates a delivery receipt a moment
                           later; real delivery receipts would arrive via a
                           provider webhook, which is out of scope here)
"""
import logging
import time

from app.config import get_settings
from app.database import update_status
from app.providers.base import ProviderError
from app.providers.factory import get_provider
from app.schemas import Channel

logger = logging.getLogger("notification_service")


def process_message(message_id: str, channel: Channel, contact: str, message: str) -> None:
    settings = get_settings()
    provider = get_provider(channel)

    try:
        result = provider.send(contact, message)
    except ProviderError as exc:
        logger.warning("message %s failed via %s: %s", message_id, channel.value, exc)
        update_status(message_id, status="failed", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - guarantee no message is left "queued" forever
        logger.exception("unexpected error sending message %s", message_id)
        update_status(message_id, status="failed", error=f"Unexpected error: {exc}")
        return

    update_status(
        message_id,
        status="sent",
        provider=result.provider_name,
        provider_message_id=result.provider_message_id,
    )

    if settings.MOCK_MODE:
        # Simulate a delivery receipt so the full status lifecycle is
        # observable end-to-end without needing real provider webhooks.
        time.sleep(1.5)
        update_status(message_id, status="delivered")
