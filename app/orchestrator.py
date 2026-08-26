"""
Notification orchestrator.

Responsibilities:
- Accept a validated send request, create one queued record per channel
  (in PostgreSQL via the storage layer), grouped under a single group_id.
- When QUEUE_ENABLED=true, publish each channel to Redis Streams for
  asynchronous worker delivery (docs/04 + 05). The API never performs slow
  provider delivery inline.
- When QUEUE_ENABLED=false (dev/fallback), dispatch delivery in-process via
  BackgroundTasks using the same provider layer (backward compatible).
- Status summaries read from the storage layer (durable source of truth).
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.config import get_settings
from app.providers.base import ProviderError, ProviderResult
from app.providers.factory import get_provider
from app.schemas import Channel, NotificationEventRequest, SendRequest
from app.storage import (
    FAILED,
    PROCESSING,
    QUEUED,
    SUBMITTED,
    get_storage,
)

logger = logging.getLogger("orchestrator")


def _safe_send(notification_id: str, channel: Channel, fn: Callable[..., ProviderResult]) -> None:
    """Run one provider send and persist its outcome. Never leaves a message
    'queued' forever: any failure is recorded with the reason."""
    storage = get_storage()
    # queued/retrying -> processing (guarded; if already processing by another
    # dispatcher, the transition returns unchanged and we proceed).
    from app.audit import record_audit

    storage.transition(notification_id, PROCESSING, actor="orchestrator")
    logger.debug("delivery started notification_id=%s channel=%s status=processing",
                 notification_id, channel.value)
    try:
        result = fn()
    except ProviderError as exc:
        logger.warning(
            "message %s failed via %s: %s (retryable=%s)",
            notification_id, channel.value, exc, getattr(exc, "retryable", False),
        )
        storage.transition(
            notification_id, FAILED, actor="orchestrator",
            error=str(exc),
        )
        row = storage.get_notification(notification_id)
        record_audit(
            user_id=row.get("created_by") if row else None,
            action="notification_failed", notification_id=notification_id,
            channel=channel.value, status="failed", result="failure",
            failure_reason=str(exc),
        )
        return
    except Exception as exc:  # noqa: BLE001 - never leave a message 'queued' forever
        logger.exception("unexpected error sending message %s", notification_id)
        storage.transition(
            notification_id, FAILED, actor="orchestrator",
            error=f"Unexpected error: {exc}",
        )
        return

    storage.transition(
        notification_id,
        to_status=SUBMITTED,
        provider=result.provider_name,
        provider_message_id=result.provider_message_id,
        actor="orchestrator",
    )
    logger.debug("provider accepted notification_id=%s channel=%s status=submitted provider=%s",
                 notification_id, channel.value, result.provider_name)
    row = storage.get_notification(notification_id)
    record_audit(
        user_id=row.get("created_by") if row else None,
        action="notification_submitted", notification_id=notification_id,
        channel=channel.value, status="submitted",
        provider=result.provider_name,
    )
    _maybe_simulate_delivery(notification_id)


def _maybe_simulate_delivery(notification_id: str) -> None:
    """In MOCK_MODE, simulate the delivery receipt a moment after the send so
    the full queued -> processing -> submitted -> delivered lifecycle is visible."""
    if not get_settings().MOCK_MODE:
        return

    def _go() -> None:
        time.sleep(1.5)
        get_storage().transition(notification_id, "delivered", actor="mock")

    threading.Thread(target=_go, daemon=True).start()


def _send_one(
    notification_id: str,
    channel: Channel,
    contact: str,
    message: str,
    template_name: Optional[str],
    template_language: Optional[str],
    template_params: Optional[Dict[str, str]],
) -> None:
    """Deliver a single message through its own channel provider (in-process path)."""
    settings = get_settings()

    def _do() -> ProviderResult:
        provider = get_provider(channel)
        if not settings.MOCK_MODE and template_name:
            return provider.send_with_template(
                contact,
                message,
                template_name=template_name,
                template_language=template_language,
                template_params=template_params,
            )
        return provider.send(contact, message)

    _safe_send(notification_id, channel, _do)


def _dispatch(notification_id: str, channel: Channel, cr, message: str,
              params: Optional[Dict], group_id: str, reference: Optional[str],
              background_tasks) -> None:
    """Dispatch one channel to the configured queue backend (or in-process)."""
    settings = get_settings()
    if settings.QUEUE_ENABLED:
        recipient = cr.contact
        if settings.QUEUE_BACKEND == "memory":
            from app.memory_queue import get_memory_queue

            get_memory_queue().publish(channel.value, notification_id, group_id,
                                       recipient, attempt=1)
        else:
            from app import queue as q

            q.publish(channel.value, notification_id, group_id, recipient, attempt=1)
        logger.info(
            "notification queued notification_id=%s channel=%s group_id=%s backend=%s",
            notification_id, channel.value, group_id, settings.QUEUE_BACKEND,
        )
        logger.debug("queue dispatch complete notification_id=%s channel=%s group_id=%s backend=%s",
                     notification_id, channel.value, group_id, settings.QUEUE_BACKEND)
    else:
        background_tasks.add_task(
            _send_one,
            notification_id,
            channel,
            cr.contact,
            message,
            cr.template_name,
            cr.template_language,
            params,
        )
        logger.debug("background dispatch scheduled notification_id=%s channel=%s group_id=%s",
                     notification_id, channel.value, group_id)


def orchestrate_send(req: SendRequest, background_tasks, message_ids: Optional[List[str]] = None,
                   parent_notification_id: Optional[str] = None) -> Dict:
    """
    Queue each channel of `req` under one group_id and dispatch delivery.

    QUEUE_ENABLED=true  → publish to Redis Streams (workers deliver).
    QUEUE_ENABLED=false → BackgroundTasks (backward compatible).

    `message_ids`, when provided, pre-assigns the public message_id per
    channel (used for durable idempotency claims before creation).
    `parent_notification_id`, when provided, links this send as a resend of
    the original notification.

    Returns the group-level summary used for the 202 response.
    """
    storage = get_storage()
    settings = get_settings()
    group_id = str(uuid.uuid4())
    logger.debug("orchestration started request_id=%s user_id=%s notification_id=%s channel_count=%d",
                 getattr(req, "_request_id", None), getattr(req, "_user_id", None), group_id,
                 len(req.channels))
    queued: List[Dict] = []

    for idx, cr in enumerate(req.channels):
        notification_id = message_ids[idx] if message_ids and idx < len(message_ids) else str(uuid.uuid4())
        params = {p.name: p.value for p in cr.template_params} if cr.template_params else None
        internal_id = storage.create_notification(
            message_id=notification_id,
            channel=cr.channel.value,
            recipient=cr.contact,
            message=req.message,
            status=QUEUED,
            group_id=group_id,
            reference=req.reference,
            template_name=cr.template_name,
            template_language=cr.template_language,
            template_params=params,
            request_id=getattr(req, "_request_id", None),
            created_by=getattr(req, "_user_id", None),
            max_attempts=settings.MAX_ATTEMPTS,
            parent_notification_id=parent_notification_id,
            resend_count=1 if parent_notification_id else 0,
        )
        logger.debug("database notification created request_id=%s user_id=%s notification_id=%s channel=%s status=queued",
                     getattr(req, "_request_id", None), getattr(req, "_user_id", None),
                     notification_id, cr.channel.value)
        from app.audit import record_audit
        record_audit(
            user_id=getattr(req, "_user_id", None),
            action="notification_created", notification_id=notification_id,
            channel=cr.channel.value, recipient=cr.contact, status=QUEUED,
            request_id=getattr(req, "_request_id", None),
        )
        queued.append({
            "message_id": notification_id,
            "channel": cr.channel.value,
            "status": "queued",
            "contact": cr.contact,
        })
        _dispatch(internal_id, cr.channel, cr, req.message, params, group_id,
                  req.reference, background_tasks)

    return {
        "message_id": group_id,
        "reference": req.reference,
        "channels": queued,
    }


def _delivery_message(channel: Channel, payload: Dict[str, Any], data: Any = None) -> str:
    """Pick the message text stored with a delivery record."""
    message = payload.get("message")
    if message:
        return message
    if isinstance(data, str) and data.strip():
        return data
    if channel == Channel.whatsapp:
        template = payload.get("template") or {}
        if template.get("id"):
            return f"[{template['id']}]"
    if channel == Channel.email and payload.get("html"):
        return payload["html"]
    return ""


def _send_delivery(
    notification_id: str,
    channel: Channel,
    payload: Dict[str, Any],
    data: Any,
) -> None:
    """Deliver one event delivery through its own channel provider (in-process path)."""

    def _do() -> ProviderResult:
        return get_provider(channel).send_delivery(payload, data)

    _safe_send(notification_id, channel, _do)


def orchestrate_event(req: NotificationEventRequest, background_tasks) -> Dict:
    """
    Queue each delivery of an event envelope under one group_id and dispatch
    delivery via the queue (or BackgroundTasks when QUEUE_ENABLED=false).
    """
    storage = get_storage()
    settings = get_settings()
    group_id = str(uuid.uuid4())
    reference = req.ref or req.request_id
    queued: List[Dict] = []

    for delivery in req.deliveries:
        notification_id = str(uuid.uuid4())
        payload = delivery.payload.model_dump(by_alias=True)
        contact = payload.get("recipient", "")
        message = _delivery_message(delivery.channel, payload, req.data)
        internal_id = storage.create_notification(
            message_id=notification_id,
            channel=delivery.channel.value,
            recipient=contact,
            message=message,
            status=QUEUED,
            group_id=group_id,
            reference=reference,
            subject=payload.get("subject"),
            template_name=(payload.get("template") or {}).get("id") if delivery.channel == Channel.whatsapp else None,
            request_id=getattr(req, "_request_id", None),
            created_by=getattr(req, "_user_id", None),
            max_attempts=settings.MAX_ATTEMPTS,
        )
        queued.append({
            "message_id": notification_id,
            "channel": delivery.channel.value,
            "status": "queued",
            "contact": contact,
        })
        if settings.QUEUE_ENABLED:
            if settings.QUEUE_BACKEND == "memory":
                from app.memory_queue import get_memory_queue

                get_memory_queue().publish(delivery.channel.value, internal_id, group_id,
                                           contact, attempt=1)
            else:
                from app import queue as q

                q.publish(delivery.channel.value, internal_id, group_id, contact, attempt=1)
            logger.info("notification queued notification_id=%s channel=%s", notification_id, delivery.channel.value)
        else:
            background_tasks.add_task(
                _send_delivery,
                internal_id,
                delivery.channel,
                payload,
                req.data,
            )

    return {
        "message_id": group_id,
        "reference": reference,
        "channels": queued,
    }


def _delivery_detail(row) -> Dict:
    """Compute elapsed time and timeout flag for a notification row.

    - elapsed_seconds: how long since the message was created.
    - timed_out: True when still queued/processing and longer than timeout.
    """
    timeout = get_settings().DELIVERY_TIMEOUT_SECONDS
    detail = {
        "delivery_timeout_seconds": timeout,
        "elapsed_seconds": None,
        "timed_out": False,
    }
    created_raw = row.get("created_at")
    if not created_raw:
        return detail
    try:
        created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
    except ValueError:
        return detail

    now = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = (now - created).total_seconds()
    detail["elapsed_seconds"] = round(elapsed, 1)

    if row.get("status") in ("queued", "processing", "retrying") and elapsed > timeout:
        detail["timed_out"] = True
    return detail


def get_group_summary(group_id: str) -> Optional[Dict]:
    """Aggregate per-channel statuses for one group into a public summary."""
    storage = get_storage()
    rows = storage.get_group(group_id)
    logger.debug("status group lookup notification_id=%s found=%s", group_id, bool(rows))
    if not rows:
        return None

    channels = []
    reference = None
    for row in rows:
        reference = row.get("reference") if row.get("reference") else reference
        detail = _delivery_detail(row)
        channels.append({
            "message_id": row["message_id"],
            "channel": row["channel"],
            "contact": row["recipient"],
            "status": row["status"],
            "provider": row.get("provider"),
            "provider_message_id": row.get("provider_message_id"),
            "error": row.get("last_error"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "read_at": row.get("read_at"),
            "acknowledged_at": row.get("acknowledged_at"),
            "acknowledgement_type": row.get("acknowledgement_type"),
            **detail,
        })

    statuses = {c["status"] for c in channels}
    if statuses == {"delivered"}:
        overall = "delivered"
    elif statuses == {"failed"} or statuses == {"dead_lettered"}:
        overall = "failed"
    elif "failed" in statuses or "dead_lettered" in statuses:
        overall = "partial"
    elif statuses <= {"queued"}:
        overall = "queued"
    else:
        overall = "sent"

    return {"message_id": group_id, "reference": reference, "status": overall, "channels": channels}


def get_message_summary(message_id: str) -> Optional[Dict]:
    storage = get_storage()
    row = storage.get_notification_by_message_id(message_id)
    logger.debug("status message lookup notification_id=%s found=%s", message_id, row is not None)
    if row is None:
        return None
    detail = _delivery_detail(row)
    return {
        "message_id": row["message_id"],
        "group_id": row.get("group_id"),
        "channel": row["channel"],
        "contact": row["recipient"],
        "status": row["status"],
        "provider": row.get("provider"),
        "provider_message_id": row.get("provider_message_id"),
        "error": row.get("last_error"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "read_at": row.get("read_at"),
        "acknowledged_at": row.get("acknowledged_at"),
        "acknowledgement_type": row.get("acknowledgement_type"),
        **detail,
    }
