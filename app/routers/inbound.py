"""
Inbound (reply) webhook.

Recipients can reply to an SMS/WhatsApp message. Providers (Vonage, Azure)
POST inbound events here. We:

1. Record the inbound message durably (inbound_messages table + audit).
2. Optionally auto-reply (configurable) so 2-way conversations work.

Providers post to the same URL with different payloads; the endpoint is
provider-agnostic and accepts a normalized shape. See docs/28 for details.

Endpoints:
    POST /api/v1/inbound                 normalized inbound message
    GET  /api/v1/inbound                 (optional) provider challenge/validation
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.audit import record_audit
from app.storage import get_storage

logger = logging.getLogger("inbound")

router = APIRouter(prefix="/api/v1/inbound", tags=["inbound"])


@router.get("", include_in_schema=False)
def inbound_validate():
    """Provider webhook validation (echo the token if provided)."""
    return JSONResponse({"status": "ok"})


@router.post("", summary="Receive a recipient reply")
async def inbound_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return _handle_inbound(payload)


def _handle_inbound(payload: Dict[str, Any]) -> JSONResponse:
    """Normalize + persist an inbound message from any provider."""
    storage = get_storage()
    # Accept both normalized and Vonage/Azure-ish shapes.
    channel = (payload.get("channel") or payload.get("channel_type") or "sms").lower()
    to = payload.get("to") or payload.get("from") or ""  # inbound: provider's 'from' is the recipient
    from_number = payload.get("from") or payload.get("to") or ""
    text = payload.get("text") or payload.get("message") or payload.get("content") or ""
    message_id = payload.get("message_id") or payload.get("message_uuid") or payload.get("id")

    # Vonage inbound: {from, to, text, message_uuid, channel}
    # Azure inbound: {from, to, message, messageId, channelType}
    if not text and payload.get("data"):
        data = payload["data"]
        if isinstance(data, dict):
            text = data.get("text") or data.get("message") or data.get("content") or text
            from_number = data.get("from") or from_number
            message_id = data.get("messageId") or data.get("message_uuid") or message_id

    if not text:
        return JSONResponse({"status": "ok", "accepted": False,
                             "error": "no text in inbound payload"})

    storage.record_inbound_message(
        channel=channel,
        from_number=str(from_number),
        to_number=str(to),
        text=str(text),
        provider_message_id=str(message_id) if message_id else None,
        raw=payload,
    )
    record_audit(
        user_id="recipient",
        action="notification_received",
        channel=channel,
        recipient=str(from_number),
        status="delivered",
        result="success",
        metadata={"inbound": True, "text_length": len(str(text))},
    )
    logger.info(
        "inbound message received channel=%s from=%s to=%s message_id=%s",
        channel, from_number, to, message_id,
    )

    # Optional auto-reply (configurable via .env INBOUND_AUTO_REPLY).
    auto_reply = _maybe_auto_reply(channel, str(from_number), str(text))
    return JSONResponse({"status": "ok", "accepted": True, "auto_reply": auto_reply})


def _maybe_auto_reply(channel: str, from_number: str, text: str) -> Optional[str]:
    """Send a configurable auto-reply to an inbound message (if enabled)."""
    from app.config import get_settings
    from app.orchestrator import orchestrate_send
    from app.schemas import Channel, ChannelRequest, SendRequest
    from fastapi import BackgroundTasks

    s = get_settings()
    if not s.INBOUND_AUTO_REPLY:
        return None
    if not s.INBOUND_AUTO_REPLY_TEXT:
        return None
    try:
        req = SendRequest(
            channels=[ChannelRequest(channel=Channel(channel), contact=from_number)],
            message=s.INBOUND_AUTO_REPLY_TEXT,
        )
        orchestrate_send(req, BackgroundTasks())
        return s.INBOUND_AUTO_REPLY_TEXT
    except Exception as exc:  # noqa: BLE001
        logger.error("auto-reply failed: %s", exc)
        return None
