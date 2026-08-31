"""
Delivery-receipt webhooks.

Azure Communication Services (ACS) Advanced Messaging posts delivery status
updates (sent/delivered/failed/read) for WhatsApp here. Without this, a send
that Azure accepts but Meta fails to deliver stays "sent" forever and the real
failure reason is invisible.

Configure Azure Event Grid subscriptions to public HTTPS endpoints:
    https://<public-host>/api/v1/whatsapp/webhook
    https://<public-host>/api/v1/email/webhook
"""
import copy
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.delivery_status import update_delivery_status
from app.storage import get_storage

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/api/v1", tags=["delivery-webhook"])

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

    details = data.get("deliveryStatusDetails")
    if isinstance(details, dict):
        return details.get("errorCode") or details.get("code"), (
            details.get("statusMessage") or details.get("message")
        )

    return None, None


def _log_event_safe(event: Dict[str, Any], channel: str = "WhatsApp") -> None:
    """Log the complete event JSON with secrets redacted (for debugging)."""
    safe = _redact(copy.deepcopy(event))
    logger.info("[%s Delivery Event] %s", channel, safe)


@router.get("/whatsapp/webhook", summary="Azure Event Grid validation handshake")
@router.get("/email/webhook", summary="Azure Event Grid validation handshake")
async def webhook_validate(request: Request):
    code = request.query_params.get("validationCode")
    if code:
        return PlainTextResponse(code)
    token = request.query_params.get("validationToken")
    if token:
        return PlainTextResponse(token)
    return JSONResponse({"error": "missing validationCode"}, status_code=400)


@router.post("/whatsapp/webhook", summary="Receive Azure WhatsApp delivery events")
@router.post("/email/webhook", summary="Receive Azure email delivery events")
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

        # ---------- Email delivery report (Azure Email Communication Service) ----------
        # eventType: Microsoft.Communication.EmailDeliveryReportReceived
        # Azure posts this when the email reaches the recipient's mailbox.
        if event_type == "Microsoft.Communication.EmailDeliveryReportReceived":
            _log_event_safe(event, channel="Email")
            data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
            message_id = data.get("messageId") or data.get("message_id") or data.get("id") or ""
            status = (data.get("status") or "").lower()
            if not message_id or not status:
                logger.debug("email delivery report ignored (missing messageId/status)")
                continue
            # "Expanded" is an intermediate distribution-list event, not a
            # result - ignore it (shared service leaves it submitted).
            if status == "expanded":
                logger.info("[Email Delivery] message_id=%s status=Expanded (ignored)", message_id)
                continue
            error_code, error_message = _extract_failure(data)
            detail = f"[{error_code}] {error_message}".strip() if error_code else (error_message or "")
            update_delivery_status(
                provider="azure_email",
                provider_message_id=message_id,
                provider_status=status,
                error=detail or None,
                channel="email",
            )
            continue

        # ---------- Delivery status events (WhatsApp) ----------
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

        error_code, error_message = _extract_failure(data)
        detail = error_message or error_code or ""
        if error_code:
            detail = f"[{error_code}] {error_message}".strip() if error_message else f"[{error_code}]"
        # Delegate to the shared delivery-status service (correlation,
        # idempotency, out-of-order guard, history + audit).
        update_delivery_status(
            provider="whatsapp",
            provider_message_id=provider_message_id,
            provider_status=status.lower(),
            error=detail or None,
            channel="whatsapp",
        )

    logger.debug("webhook response status=ok event_count=%d", len(events))
    return JSONResponse({"status": "ok"})
