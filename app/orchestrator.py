"""
Notification orchestrator.

Responsibilities:
- Accept a validated send request, create one queued record per channel,
  grouped under a single group_id.
- Route each channel to ITS OWN provider only (sms -> sms provider,
  whatsapp -> whatsapp provider, email -> email provider). No cross-channel
  access.
- Update per-channel status in the background and mark failures with reason.

Adding a new channel later = add a provider + register it in the factory;
the orchestrator needs no change.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.config import get_settings
from app.database import create_message, get_group, get_message, update_status
from app.providers.base import ProviderError, ProviderResult
from app.providers.factory import get_provider
from app.schemas import Channel, NotificationEventRequest, SendRequest

logger = logging.getLogger("orchestrator")


def _safe_send(message_id: str, channel: Channel, fn: Callable[..., ProviderResult]) -> None:
    """
    Run one provider send and persist its outcome. Never leaves a message
    'queued' forever: any failure is recorded with the reason.
    """
    try:
        result = fn()
    except ProviderError as exc:
        logger.warning("message %s failed via %s: %s", message_id, channel.value, exc)
        update_status(message_id, status="failed", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - never leave a message 'queued' forever
        logger.exception("unexpected error sending message %s", message_id)
        update_status(message_id, status="failed", error=f"Unexpected error: {exc}")
        return

    update_status(
        message_id,
        status=result.status,
        provider=result.provider_name,
        provider_message_id=result.provider_message_id,
    )
    _maybe_simulate_delivery(message_id)


def _maybe_simulate_delivery(message_id: str) -> None:
    """In MOCK_MODE, simulate the delivery receipt a moment after the send so
    the full queued -> sent -> delivered lifecycle stays observable."""
    if not get_settings().MOCK_MODE:
        return

    def _go() -> None:
        time.sleep(1.5)
        update_status(message_id, status="delivered")

    threading.Thread(target=_go, daemon=True).start()


def _send_one(
    message_id: str,
    channel: Channel,
    contact: str,
    message: str,
    template_name: Optional[str],
    template_language: Optional[str],
    template_params: Optional[Dict[str, str]],
) -> None:
    """Deliver a single message through its own channel provider."""
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

    _safe_send(message_id, channel, _do)


def orchestrate_send(req: SendRequest, background_tasks) -> Dict:
    """
    Queue each channel of `req` under one group_id and dispatch delivery via
    FastAPI BackgroundTasks (runs after the response is sent).

    Returns the group-level summary used for the 202 response.
    """
    group_id = str(uuid.uuid4())
    queued: List[Dict] = []

    for cr in req.channels:
        message_id = str(uuid.uuid4())
        params = {p.name: p.value for p in cr.template_params} if cr.template_params else None
        create_message(
            message_id=message_id,
            channel=cr.channel.value,
            contact=cr.contact,
            message=req.message,
            status="queued",
            group_id=group_id,
            reference=req.reference,
        )
        queued.append({
            "message_id": message_id,
            "channel": cr.channel.value,
            "status": "queued",
            "contact": cr.contact,
        })
        background_tasks.add_task(
            _send_one,
            message_id,
            cr.channel,
            cr.contact,
            req.message,
            cr.template_name,
            cr.template_language,
            params,
        )

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
    message_id: str,
    channel: Channel,
    payload: Dict[str, Any],
    data: Any,
) -> None:
    """Deliver one event delivery through its own channel provider."""

    def _do() -> ProviderResult:
        return get_provider(channel).send_delivery(payload, data)

    _safe_send(message_id, channel, _do)


def orchestrate_event(req: NotificationEventRequest, background_tasks) -> Dict:
    """
    Queue each delivery of an event envelope under one group_id and dispatch
    delivery via FastAPI BackgroundTasks (runs after the response is sent).

    Each delivery carries its own recipient and channel-specific payload
    (WhatsApp template, SMS message, rich email with cc/bcc/attachments).
    """
    group_id = str(uuid.uuid4())
    reference = req.ref or req.request_id
    queued: List[Dict] = []

    for delivery in req.deliveries:
        message_id = str(uuid.uuid4())
        payload = delivery.payload.model_dump(by_alias=True)
        contact = payload.get("recipient", "")
        message = _delivery_message(delivery.channel, payload, req.data)
        create_message(
            message_id=message_id,
            channel=delivery.channel.value,
            contact=contact,
            message=message,
            status="queued",
            group_id=group_id,
            reference=reference,
        )
        queued.append({
            "message_id": message_id,
            "channel": delivery.channel.value,
            "status": "queued",
            "contact": contact,
        })
        background_tasks.add_task(
            _send_delivery,
            message_id,
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
    """Compute elapsed time and timeout flag for a message row.

    - elapsed_seconds: how long since the message was created.
    - timed_out: True when the message is still queued/sent and has been
      waiting longer than DELIVERY_TIMEOUT_SECONDS. This tells callers when
      to stop polling and treat the send as stuck.
    """
    timeout = get_settings().DELIVERY_TIMEOUT_SECONDS
    detail = {
        "delivery_timeout_seconds": timeout,
        "elapsed_seconds": None,
        "timed_out": False,
    }
    created_raw = row["created_at"]
    if not created_raw:
        return detail
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return detail

    now = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = (now - created).total_seconds()
    detail["elapsed_seconds"] = round(elapsed, 1)

    if row["status"] in ("queued", "sent") and elapsed > timeout:
        detail["timed_out"] = True
    return detail


def get_group_summary(group_id: str) -> Optional[Dict]:
    """Aggregate per-channel statuses for one group into a public summary."""
    rows = get_group(group_id)
    if not rows:
        return None

    channels = []
    reference = None
    for row in rows:
        reference = row["reference"] if row["reference"] else reference
        detail = _delivery_detail(row)
        channels.append({
            "message_id": row["message_id"],
            "channel": row["channel"],
            "contact": row["contact"],
            "status": row["status"],
            "provider": row["provider"],
            "provider_message_id": row["provider_message_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            **detail,
        })

    statuses = {c["status"] for c in channels}
    if statuses == {"delivered"}:
        overall = "delivered"
    elif statuses == {"failed"}:
        overall = "failed"
    elif "failed" in statuses:
        overall = "partial"
    elif statuses <= {"queued"}:
        overall = "queued"
    else:
        overall = "sent"

    return {"message_id": group_id, "reference": reference, "status": overall, "channels": channels}


def get_message_summary(message_id: str) -> Optional[Dict]:
    row = get_message(message_id)
    if row is None:
        return None
    detail = _delivery_detail(row)
    return {
        "message_id": row["message_id"],
        "group_id": row["group_id"],
        "channel": row["channel"],
        "contact": row["contact"],
        "status": row["status"],
        "provider": row["provider"],
        "provider_message_id": row["provider_message_id"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **detail,
    }
