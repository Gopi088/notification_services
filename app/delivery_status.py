"""
Shared delivery-status service used by all channel webhooks (SMS, WhatsApp,
Email). Providers call update_delivery_status() which locates the notification
by provider_message_id, validates the transition, and persists the result
atomically with history + audit.

Business rules:
- Always locate by provider_message_id – never by phone/email/contact.
- Idempotent: duplicate callbacks are no-ops (storage.transition rejects invalid
  backward transitions).
- Out-of-order safe: an older state (e.g. "sent" after "delivered") is rejected
  by the same guard.
- Sets delivered_at / read_at automatically.
- Records one webhook event per call.
"""

import logging
from typing import Optional

from app.audit import record_audit
from app.providers.base import sanitize_provider_error
from app.storage import get_storage

logger = logging.getLogger("delivery_status")

# Channel-agnostic status mapping: provider status -> our internal status.
# Each map entry is (target_status, is_terminal) where is_terminal signals
# that no further transitions are expected for this channel.
STATUS_MAP: dict[str, Optional[str]] = {
    # Twilio
    "queued": "submitted",    # provider accepted the message into its queue
    "accepted": "submitted",
    "sending": "processing",
    "sent": "submitted",      # carrier accepted == provider accepted
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
    # Azure / Vonage
    "accepted": None,
    "submitted": None,
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
    "rejected": "failed",
    "bounced": "failed",
    "suppressed": "failed",
}


def update_delivery_status(
    provider: str,
    provider_message_id: str,
    provider_status: str,
    error: Optional[str] = None,
    channel: Optional[str] = None,
) -> bool:
    """
    Process one delivery-status update from a provider.

    Locates the notification by provider_message_id, maps the provider status
    to our internal status, and persists the transition. Returns True when the
    notification was found and the transition was attempted (even if it was a
    no-op due to idempotency). Returns False when the notification is unknown
    (provider_message_id not found).
    """
    error = sanitize_provider_error(error) if error else None
    storage = get_storage()
    notif = storage.get_by_provider_message_id(provider_message_id)
    if notif is None:
        storage.record_webhook_event(
            provider=provider, provider_message_id=provider_message_id,
            status=provider_status,
            payload={"error": error} if error else {},
        )
        logger.info("[%s] status for unknown message %s status=%s", provider, provider_message_id, provider_status)
        return False

    # A provider identifier is only meaningful within that provider/channel.
    # Refuse a mismatched callback rather than allowing a guessed identifier
    # to mutate a notification owned by another integration.
    stored_provider = (notif.get("provider") or "").lower()
    provider_family = provider.lower().split("_", 1)[0]
    stored_family = stored_provider.split("_", 1)[0]
    if stored_provider and stored_family != provider_family:
        logger.warning("webhook provider mismatch sid=%s expected=%s received=%s",
                       provider_message_id, stored_provider, provider)
        return False
    if channel and notif.get("channel") != channel:
        logger.warning("webhook channel mismatch sid=%s expected=%s received=%s",
                       provider_message_id, notif.get("channel"), channel)
        return False

    target = STATUS_MAP.get(provider_status.lower())
    if target is None:
        logger.debug("[%s] status %s mapped to None (no change) sid=%s", provider, provider_status, provider_message_id)
        storage.record_webhook_event(
            provider=provider, provider_message_id=provider_message_id,
            status=provider_status, payload={"error": error} if error else {},
        )
        return True

    chan = channel or notif.get("channel")
    logger.info("[%s] status update sid=%s from=%s to=%s channel=%s",
                provider, provider_message_id, notif["status"], target, chan)

    storage.record_webhook_event(
        provider=provider, provider_message_id=provider_message_id,
        status=provider_status,
        payload={"error": error} if error else {},
    )

    if target == "delivered":
        storage.transition(
            notif["id"], "delivered", actor="webhook",
            provider=provider, provider_message_id=provider_message_id,
            error=error,
        )
        record_audit(
            user_id=notif.get("created_by"),
            action="notification_delivered",
            notification_id=notif.get("message_id") or notif["id"],
            channel=chan, status="delivered", provider=provider,
        )
        logger.info("[%s] message %s delivered (channel=%s)", provider, provider_message_id, chan)
    elif target == "sent":
        storage.transition(
            notif["id"], "sent", actor="webhook",
            provider=provider, provider_message_id=provider_message_id,
        )
        logger.info("[%s] message %s sent to carrier (channel=%s)", provider, provider_message_id, chan)
    elif target == "read":
        # Ensure delivered before read (idempotent if already delivered)
        storage.transition(notif["id"], "delivered", actor="webhook",
                           provider=provider, provider_message_id=provider_message_id)
        storage.transition(notif["id"], "read", actor="webhook",
                           provider=provider, provider_message_id=provider_message_id)
        record_audit(
            user_id=notif.get("created_by"),
            action="notification_read",
            notification_id=notif.get("message_id") or notif["id"],
            channel=chan, status="read", provider=provider,
        )
        logger.info("[%s] message %s read (channel=%s)", provider, provider_message_id, chan)
    elif target == "failed":
        detail = error or f"{provider} delivery failed"
        storage.transition(
            notif["id"], "failed", actor="webhook",
            provider=provider, provider_message_id=provider_message_id,
            error=detail,
        )
        record_audit(
            user_id=notif.get("created_by"),
            action="notification_failed",
            notification_id=notif.get("message_id") or notif["id"],
            channel=chan, status="failed", result="failure",
            failure_reason=detail,
        )
        logger.warning("[%s] message %s failed (channel=%s) error=%s", provider, provider_message_id, chan, detail)

    return True
