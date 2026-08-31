"""
Twilio delivery-status webhooks (SMS + WhatsApp).

Twilio POSTs form-encoded (application/x-www-form-urlencoded) status updates to
the `StatusCallback` URL when a message's delivery status changes. Dedicated
endpoints are provided:

  POST /api/v1/twilio/sms/status       (SMS - TWILIO_SMS_STATUS_CALLBACK_URL)
  POST /api/v1/twilio/whatsapp/status  (WhatsApp - TWILIO_WHATSAPP_STATUS_CALLBACK_URL)

Legacy aliases remain working:
  POST /api/v1/twilio/status
  POST /api/v1/sms/webhook

All endpoints validate the Twilio signature (X-Twilio-Signature) in production,
read MessageSid + MessageStatus, and delegate to the shared
`app.delivery_status.update_delivery_status` service so the state machine,
history and audit are identical across channels. WhatsApp additionally supports
`EventType=READ` -> internal `read`.

Status mapping (Twilio -> internal):
  queued -> queued      sending -> processing   sent -> submitted
  delivered -> delivered  failed -> failed      undelivered -> failed
"""
import base64
import hashlib
import hmac
import logging
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.delivery_status import update_delivery_status

logger = logging.getLogger("twilio.webhook")
router = APIRouter(prefix="/api/v1", tags=["twilio-webhook"])


def _valid_signature(request: Request, form_body: str, url: str) -> bool:
    """Validate Twilio's X-Twilio-Signature (HMAC-SHA1 of URL + POST params)."""
    settings = get_settings()
    if not settings.TWILIO_AUTH_TOKEN:
        return False
    expected = request.headers.get("X-Twilio-Signature", "")
    if not expected:
        return False
    params = "".join(f"{k}{v}" for k, v in sorted(parse_qsl(form_body, keep_blank_values=True)))
    payload = (url + params).encode()
    digest = base64.b64encode(
        hmac.new(settings.TWILIO_AUTH_TOKEN.encode(), payload, hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(digest, expected)


def _error_detail(form: dict) -> str:
    error_code = form.get("ErrorCode") or ""
    error_message = form.get("ErrorMessage") or ""
    if error_code:
        return f"[{error_code}] {error_message}".strip() if error_message else f"[{error_code}]"
    return error_message or ""


async def _handle(request: Request, channel: Optional[str] = None) -> JSONResponse:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v for k, v in parse_qsl(raw, keep_blank_values=True)}

    if not get_settings().MOCK_MODE and not _valid_signature(request, raw, str(request.url)):
        logger.warning("twilio webhook rejected: invalid signature")
        return JSONResponse({"status": "rejected"}, status_code=403)

    message_sid = (form.get("MessageSid") or form.get("SmsSid") or "").strip()
    message_status = (form.get("MessageStatus") or "").strip().lower()
    # WhatsApp read receipts may arrive as EventType=READ (with MessageStatus
    # delivered); normalize to our `read` status.
    if (form.get("EventType") or "").upper() == "READ":
        message_status = "read"

    if not message_sid or not message_status:
        logger.debug("twilio webhook ignored (missing MessageSid/MessageStatus)")
        return JSONResponse({"status": "ok"})

    logger.debug(
        "twilio status callback received provider_message_id=%s provider_status=%s channel=%s event_type=%s",
        message_sid, message_status, channel or "?", form.get("EventType") or "",
    )

    detail = _error_detail(form)
    update_delivery_status(
        provider="twilio",
        provider_message_id=message_sid,
        provider_status=message_status,
        error=detail or None,
        channel=channel,
    )
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------- SMS status
@router.get("/twilio/sms/status", summary="Twilio SMS status callback (GET) - acknowledge")
async def twilio_sms_status_get(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.post("/twilio/sms/status", summary="Receive Twilio SMS delivery status updates")
async def twilio_sms_status(request: Request) -> JSONResponse:
    return await _handle(request, channel="sms")


# ------------------------------------------------------------ WhatsApp status
@router.get("/twilio/whatsapp/status", summary="Twilio WhatsApp status callback (GET) - acknowledge")
async def twilio_whatsapp_status_get(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.post("/twilio/whatsapp/status", summary="Receive Twilio WhatsApp delivery status updates")
async def twilio_whatsapp_status(request: Request) -> JSONResponse:
    return await _handle(request, channel="whatsapp")


# --------------------------------------------------------------- legacy aliases
@router.get("/twilio/status", summary="Twilio status callback (GET) - acknowledge (legacy)")
async def twilio_status_get(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.post("/twilio/status", summary="Receive Twilio delivery status updates (legacy)")
async def twilio_status(request: Request) -> JSONResponse:
    return await _handle(request)


@router.get("/sms/webhook", summary="SMS status callback (GET) - acknowledge (legacy)")
async def sms_webhook_get(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.post("/sms/webhook", summary="Receive Twilio SMS delivery status updates (legacy)")
async def sms_webhook(request: Request) -> JSONResponse:
    return await _handle(request, channel="sms")
