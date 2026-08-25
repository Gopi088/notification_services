"""
Versioned public API (v1).

Design:
- API versioning: all routes live under /api/v1. Future breaking changes go
  under /api/v2 while /api/v1 keeps working.
- Medium separation: a send request lists one or more channels, each with its
  own contact + optional external template. Internals (providers, DB) never
  leak into responses.
- Uniform envelope: {success, data-or-error} so callers parse one shape.
"""
from fastapi import APIRouter, BackgroundTasks, Depends

from app import __version__
from app.auth import require_api_key
from app.config import get_settings
from app.errors import NotFoundError, ValidationError
from app.orchestrator import (
    get_group_summary,
    get_message_summary,
    orchestrate_event,
    orchestrate_send,
)
from app.schemas import (
    ChannelStatus,
    ChannelQueued,
    ErrorResponse,
    HealthResponse,
    NotificationEventRequest,
    SendRequest,
    SendResponse,
    StatusResponse,
)
from app.validation import ContactValidationError, validate_contact

router = APIRouter(prefix="/api/v1", tags=["v1"])

_settings = get_settings()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and configuration overview",
    dependencies=[Depends(require_api_key)],
)
def health() -> HealthResponse:
    return HealthResponse(
        service=_settings.APP_NAME,
        version=__version__,
        mock_mode=_settings.MOCK_MODE,
    )


@router.post(
    "/notifications/send",
    response_model=SendResponse,
    status_code=202,
    summary="Send a notification over one or more channels",
    description=(
        "Each entry in `channels` targets a separate medium (whatsapp/sms/email). "
        "A channel never falls through to another medium. Optional per-channel "
        "external templates control how the message is rendered."
    ),
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key)],
)
def send(payload: SendRequest, background_tasks: BackgroundTasks) -> SendResponse:
    # Validate every channel's contact upfront so a bad phone on one channel
    # rejects the whole request before anything is queued.
    for cr in payload.channels:
        try:
            validate_contact(cr.channel, cr.contact)
        except ContactValidationError as exc:
            raise ValidationError(str(exc), field="channels") from exc

    summary = orchestrate_send(payload, background_tasks)
    return SendResponse(
        success=True,
        message_id=summary["message_id"],
        reference=summary.get("reference"),
        status="queued",
        channels=[ChannelQueued(**c) for c in summary["channels"]],
    )


@router.post(
    "/notifications/event",
    response_model=SendResponse,
    status_code=202,
    summary="Send an event-driven notification (one delivery per channel)",
    description=(
        "Accepts an event envelope (request_id / event_type / ref / data) whose "
        "`deliveries` list targets each channel with its own payload: WhatsApp "
        "supports a Meta template id + language, SMS takes a plain message, and "
        "Email supports subject, html, cc, bcc, replyTo and attachments."
    ),
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key)],
)
def send_event(payload: NotificationEventRequest, background_tasks: BackgroundTasks) -> SendResponse:
    # Validate every delivery's recipient upfront so a bad phone on one channel
    # rejects the whole request before anything is queued.
    for delivery in payload.deliveries:
        try:
            validate_contact(delivery.channel, delivery.payload.recipient)
        except ContactValidationError as exc:
            raise ValidationError(str(exc), field="deliveries") from exc

    summary = orchestrate_event(payload, background_tasks)
    return SendResponse(
        success=True,
        message_id=summary["message_id"],
        reference=summary.get("reference"),
        status="queued",
        channels=[ChannelQueued(**c) for c in summary["channels"]],
    )


@router.get(
    "/notifications/{notification_id}/status",
    response_model=StatusResponse,
    summary="Delivery status for a grouped send (or a single channel message)",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key)],
)
def status(notification_id: str) -> StatusResponse:
    # Prefer the group view; fall back to a legacy single message id.
    group = get_group_summary(notification_id)
    if group is not None:
        return StatusResponse(
            success=True,
            message_id=group["message_id"],
            reference=group.get("reference"),
            status=group["status"],
            channels=[ChannelStatus(**c) for c in group["channels"]],
        )

    single = get_message_summary(notification_id)
    if single is not None:
        return StatusResponse(
            success=True,
            message_id=single["message_id"],
            status=single["status"],
            channels=[
                ChannelStatus(
                    message_id=single["message_id"],
                    channel=single["channel"],
                    contact=single["contact"],
                    status=single["status"],
                    provider=single["provider"],
                    provider_message_id=single["provider_message_id"],
                    error=single["error"],
                    created_at=single["created_at"],
                    updated_at=single["updated_at"],
                    attempt_count=single.get("attempt_count", 0),
                    elapsed_seconds=single.get("elapsed_seconds"),
                    timed_out=single.get("timed_out", False),
                    delivery_timeout_seconds=single.get("delivery_timeout_seconds"),
                )
            ],
        )

    raise NotFoundError(f"No notification found with id '{notification_id}'")
