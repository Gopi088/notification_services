"""
Delivery-receipt webhook.

Azure Communication Services (ACS) Advanced Messaging posts delivery status
updates (sent/delivered/failed/read) for WhatsApp here. Without this, a send
that Azure accepts but Meta fails to deliver stays "sent" forever and the real
failure reason is invisible.

Endpoint URL (configure in Azure portal -> your ACS resource -> Events ->
Advanced Message Delivery Status Updated -> Webhook):
    https://<public-host>/api/v1/whatsapp/webhook
"""
import copy
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.audit import record_audit
from app.storage import get_storage

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp-webhook"])

# Keys whose values should be redacted in logs.
_SECRET_KEYS = frozenset({
    "authorization", "accesskey", "access_key", "secret", "password",
    "token", "connection_string", "connectionstring", "signingkey",
    "api_key", "apikey",
})


def _redact(value: Any, key: str = "") -> Any:
    """Recursively redact known secret fields from a JSON-serialisable value."""
    if isinstance(value, dict):
        return {k: "***" if k.lower() in _SECRET_KEYS else _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def _extract_failure(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (error_code, error_message) from a delivery event's data object.

    Tries several known Azure ACS event schemas:
      - data.error       -> {code, message, details:[...]}
      - data.errorCode   + data.errorMessage
      - data.error       + data.error_description
      - data.deliveryStatus / data.statusReason
      - data.error       as a string
    """
    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code") or ""
        message = error.get("message") or ""
        # If no direct message, look in the first detail entry
        if not message:
            details = error.get("details")
            if isinstance(details, list) and details:
                for d in details:
                    if isinstance(d, dict):
                        message = d.get("message") or ""
                        if message:
                            break
        return code or None, message or None

    if error is not None:
        # error is a bare string or other type
        return None, str(error)

    # Fallback: try other common field names
    code = data.get("errorCode") or data.get("error_code")
    message = data.get("errorMessage") or data.get("error_message") or data.get("reason")
    if message or code:
        return code, message

    # Try statusReason / deliveryStatus
    if data.get("deliveryStatus"):
        return None, data.get("deliveryStatus")
    if data.get("statusReason"):
        return None, data.get("statusReason")

    return None, None


def _log_event_safe(event: Dict[str, Any]) -> None:
    """Log the complete event JSON with secrets redacted (for debugging)."""
    safe = _redact(copy.deepcopy(event))
    logger.info("[WhatsApp Delivery Event] %s", safe)


@router.get("/webhook", summary="Azure Event Grid validation handshake")
async def webhook_validate(request: Request):
    code = request.query_params.get("validationCode")
    if code:
        return PlainTextResponse(code)
    token = request.query_params.get("validationToken")
    if token:
        return PlainTextResponse(token)
    return JSONResponse({"error": "missing validationCode"}, status_code=400)


@router.post("/webhook", summary="Receive WhatsApp delivery status updates and Event Grid validation")
async def webhook_receive(request: Request):
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    logger.debug("webhook request parsed event_count=%d", len(events))

    for event in events:
        event_type = event.get("eventType", "")

        # ---------- Event Grid subscription validation ----------
        if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
            data = event.get("data", {})
            code = data.get("validationCode", "")
            logger.info(
                "Event Grid validation request received: eventType=%s validationCode=%s",
                event_type, code,
            )
            if code:
                logger.info("validationResponse returned: %s", code)
                return JSONResponse({"validationResponse": code})
            logger.warning("SubscriptionValidationEvent missing validationCode")
            continue

        # ---------- Delivery status events ----------
        data = event.get("data") if isinstance(event, dict) and isinstance(event.get("data"), dict) else event
        if data.get("channelType") not in (None, "whatsapp"):
            continue

        provider_message_id = data.get("messageId") or data.get("message_id")
        status = data.get("status")
        if not provider_message_id or not status:
            continue
        logger.debug("webhook delivery received notification_id=%s channel=whatsapp status=%s",
                     provider_message_id, status.lower())

        # Log the full event safely for debugging
        _log_event_safe(event)

        status_lower = status.lower()
        storage = get_storage()
        if status_lower == "delivered":
            notif = storage.get_by_provider_message_id(provider_message_id)
            if notif:
                storage.transition(notif["id"], "delivered", actor="webhook")
            storage.record_webhook_event(
                provider="whatsapp", provider_message_id=provider_message_id,
                status="delivered", payload=data,
            )
            record_audit(
                user_id=notif.get("created_by") if notif else None,
                action="notification_delivered", notification_id=notif["id"] if notif else None,
                channel="whatsapp", status="delivered",
            )
            logger.info(
                "[WhatsApp Delivery Event] message_id=%s status=Delivered channel=whatsapp",
                provider_message_id,
            )
        elif status_lower in ("failed", "undelivered"):
            error_code, error_message = _extract_failure(data)
            detail = error_message or error_code or "unknown failure (no error details in event)"
            if error_code:
                detail = f"[{error_code}] {error_message}" if error_message else f"[{error_code}]"
            notif = storage.get_by_provider_message_id(provider_message_id)
            if notif:
                storage.transition(
                    notif["id"], "failed", actor="webhook",
                    error=f"WhatsApp delivery failed: {detail}",
                )
            storage.record_webhook_event(
                provider="whatsapp", provider_message_id=provider_message_id,
                status="failed", error_code=error_code, error_message=error_message,
                payload=data,
            )
            record_audit(
                user_id=notif.get("created_by") if notif else None,
                action="notification_failed", notification_id=notif["id"] if notif else None,
                channel="whatsapp", status="failed", result="failure",
                failure_reason=detail,
            )
            logger.warning(
                "[WhatsApp Delivery Event] message_id=%s status=Failed channel=whatsapp "
                "error_code=%s error_message=%s",
                provider_message_id, error_code, error_message,
            )
        elif status_lower == "read":
            notif = storage.get_by_provider_message_id(provider_message_id)
            if notif:
                storage.transition(notif["id"], "delivered", actor="webhook")
            storage.record_webhook_event(
                provider="whatsapp", provider_message_id=provider_message_id,
                status="read", payload=data,
            )
            logger.info(
                "[WhatsApp Delivery Event] message_id=%s status=Read channel=whatsapp",
                provider_message_id,
            )
        else:
            storage.record_webhook_event(
                provider="whatsapp", provider_message_id=provider_message_id,
                status=status, payload=data,
            )
            logger.info(
                "[WhatsApp Delivery Event] message_id=%s status=%s channel=whatsapp (unhandled)",
                provider_message_id, status,
            )

    logger.debug("webhook response status=ok event_count=%d", len(events))
    return JSONResponse({"status": "ok"})
