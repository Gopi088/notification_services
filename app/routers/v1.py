"""
Versioned public API (v1).

Design:
- API versioning: all routes live under /api/v1. Future breaking changes go
  under /api/v2 while /api/v1 keeps working.
- Medium separation: a send request lists one or more channels, each with its
  own contact + optional external template. Internals (providers, DB) never
  leak into responses.
- Request correlation: request_id generated per request and propagated to the
  orchestrator -> queue -> worker -> DB.
- Idempotency + rate limiting enforced when enabled.
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import __version__
from app.auth import user_id_from_request
from app.config import get_settings
from app.idempotency import (
    check_redis,
    derive_key,
    normalize_client_key,
    payload_hash,
)
from app.logging_config import new_request_id
from app.orchestrator import (
    get_group_summary,
    get_message_summary,
    orchestrate_event,
    orchestrate_send,
)
from app.ratelimit import check_api_send, check_recipient
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
from app.audit import record_audit
from app.storage import get_storage
from app.validation import ContactValidationError, validate_contact

logger = logging.getLogger("api.v1")

router = APIRouter(prefix="/api/v1", tags=["v1"])

_settings = get_settings()


def _send_error(code: str, message: str, field: Optional[str] = None) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=ErrorResponse(error={"code": code, "message": message, "field": field}).model_dump(),
    )


def _get_request_id(request: Request) -> str:
    existing = request.headers.get("X-Request-ID")
    return new_request_id(existing)


def _store_idempotency_key(key: str, summary: dict) -> None:
    """Persist the idempotency key (durable) + Redis fast path after enqueue."""
    from app.idempotency import check_durable, store_redis

    first_id = summary["channels"][0]["message_id"]
    notif_id, is_new = check_durable(key, "stored-after-send")
    if notif_id and is_new:
        store_redis(key, notif_id)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and configuration overview",
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
        "Send an optional `Idempotency-Key` header to prevent duplicate sends."
    ),
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def send(payload: SendRequest, background_tasks: BackgroundTasks, request: Request) -> SendResponse:
    request_id = _get_request_id(request)
    user_id = user_id_from_request(request)
    logger.info("API request received method=send channels=%s request_id=%s user_id=%s",
                ",".join(c.channel.value for c in payload.channels), request_id, user_id)
    payload._request_id = request_id
    payload._user_id = user_id
    logger.debug("send request parsed request_id=%s user_id=%s channel_count=%d",
                 request_id, user_id, len(payload.channels))

    key_id = request.headers.get("X-API-Key", "anon")[:16]

    # ---- Rate limiting ----
    rl = check_api_send(key_id)
    logger.debug("send rate limit checked request_id=%s user_id=%s allowed=%s remaining=%d",
                 request_id, user_id, rl.allowed, rl.remaining)
    if not rl.allowed:
        logger.warning("rate limited send key=%s request_id=%s", key_id, request_id)
        record_audit(
            user_id=key_id, action="rate_limit_exceeded", result="failure",
            failure_reason="send limit exceeded", request_id=request_id,
        )
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(rl.reset_seconds)},
            detail=ErrorResponse(error={"code": "rate_limited",
                                          "message": "Send limit exceeded.",
                                          "field": None}).model_dump(),
        )

    # ---- Idempotency (client key or derived) ----
    idem_key = None
    header_key = request.headers.get("Idempotency-Key", "").strip()
    if header_key:
        try:
            idem_key = normalize_client_key(header_key)
        except ValueError as exc:
            raise _send_error("validation_error", str(exc), field="Idempotency-Key") from exc
    else:
        idem_key = derive_key(
            ",".join(c.channel.value for c in payload.channels),
            ",".join(c.contact for c in payload.channels),
            payload.message,
            payload.reference,
        )
    logger.debug("send idempotency resolved request_id=%s user_id=%s source=%s",
                 request_id, user_id, "client" if header_key else "derived")

    # Redis fast-path duplicate check.
    existing_id = check_redis(idem_key)
    if existing_id:
        existing = get_message_summary(existing_id)
        if existing:
            logger.warning("duplicate notification detected idempotency_key=%s request_id=%s",
                           idem_key, request_id)
            record_audit(
                user_id=key_id, action="duplicate_notification_attempted",
                notification_id=existing_id, status=existing["status"],
                request_id=request_id, result="already_exists",
            )
            raise HTTPException(
                status_code=202,
                headers={"X-Idempotent-Replay": "true"},
                detail=SendResponse(
                    message_id=existing["message_id"],
                    reference=existing.get("reference"),
                    status=existing["status"],
                    channels=[ChannelQueued(
                        message_id=existing["message_id"],
                        channel=existing["channel"],
                        status=existing["status"],
                        contact=existing["contact"],
                    )],
                ).model_dump(),
            )

    # ---- Per-recipient rate limiting ----
    for cr in payload.channels:
        if cr.channel.value in ("whatsapp", "sms"):
            rl_r = check_recipient(cr.contact)
            if not rl_r.allowed:
                raise HTTPException(
                    status_code=429,
                    headers={"Retry-After": str(rl_r.reset_seconds)},
                    detail=ErrorResponse(error={"code": "rate_limited",
                                                  "message": "Recipient limit exceeded.",
                                                  "field": "channels"}).model_dump(),
                )

    # ---- Contact validation ----
    for cr in payload.channels:
        try:
            validate_contact(cr.channel, cr.contact)
        except ContactValidationError as exc:
            raise _send_error("validation_error", str(exc), field="channels") from exc
        logger.debug("send validation passed request_id=%s user_id=%s channel=%s",
                     request_id, user_id, cr.channel.value)

    # ---- Durable idempotency claim BEFORE creating the notification ----
    # The DB unique constraint on idempotency_keys.key is the concurrency
    # mutex: only one of N concurrent requests with the same key wins the
    # insert; the rest replay the original result. This prevents duplicate
    # notifications even under 100 concurrent identical requests.
    from app.idempotency import claim_idempotency_key, store_redis

    pre_ids = [str(uuid.uuid4()) for _ in payload.channels]
    ph = payload_hash(payload.model_dump())
    # ---- Check for an existing notification with this key ----
    import time as _time

    existing_row = get_storage().find_idempotency_key_row(idem_key)
    original_nid = existing_row.get("notification_id") if existing_row else None
    original = get_message_summary(original_nid) if original_nid else None

    if existing_row and original and not payload.resend:
        # Accidental duplicate: return the existing notification, DO NOT resend.
        store_redis(idem_key, original_nid)
        logger.warning("duplicate notification detected idempotency_key=%s request_id=%s",
                       idem_key, request_id)
        record_audit(
            user_id=key_id, action="duplicate_notification_attempted",
            notification_id=original_nid, status=original["status"],
            request_id=request_id, result="already_exists",
        )
        raise HTTPException(
            status_code=202,
            headers={"X-Idempotent-Replay": "true"},
            detail=SendResponse(
                message_id=original["message_id"],
                reference=original.get("reference"),
                status=original["status"],
                channels=[ChannelQueued(
                    message_id=original["message_id"],
                    channel=original["channel"],
                    status=original["status"],
                    contact=original["contact"],
                )],
            ).model_dump(),
        )

    if existing_row and original and payload.resend:
        # Explicit resend: create a NEW notification linked to the original.
        # Each resend uses a fresh idempotency key so it cannot be accidentally
        # deduplicated against the original or another resend.
        resend_key = f"{idem_key}:resend:{uuid.uuid4().hex[:12]}"
        logger.warning("resend requested idempotency_key=%s original=%s request_id=%s",
                       idem_key, original_nid, request_id)
        record_audit(
            user_id=key_id, action="notification_resend_requested",
            notification_id=original_nid, channel=payload.channels[0].channel.value if payload.channels else None,
            request_id=request_id, result="queued",
        )

        # Link the resend to the original's internal id (parent_notification_id
        # is a foreign key onto notifications.id, which may differ from the
        # public message_id recorded in the idempotency keys table).
        original_row = get_storage().get_notification_by_message_id(original_nid)
        parent_id = original_row["id"] if original_row else original_nid

        resend_ids = [str(uuid.uuid4()) for _ in payload.channels]
        claim_idempotency_key(resend_key, resend_ids[0], ph)
        summary = orchestrate_send(payload, background_tasks, message_ids=resend_ids,
                                   parent_notification_id=parent_id)
        store_redis(resend_key, resend_ids[0])
        new_mid = summary["channels"][0]["message_id"]
        record_audit(
            user_id=key_id, action="notification_resent",
            notification_id=new_mid,
            request_id=request_id, channel=payload.channels[0].channel.value if payload.channels else None,
            metadata={"original_notification_id": original_nid},
        )
        logger.info("resent notification original=%s new=%s request_id=%s",
                    original_nid, new_mid, request_id)
        return SendResponse(
            success=True,
            message_id=summary["message_id"],
            reference=summary.get("reference"),
            status="queued",
            channels=[ChannelQueued(**c) for c in summary["channels"]],
        )

    # ---- Claim the idempotency key (new request) ----
    claimed = claim_idempotency_key(idem_key, pre_ids[0], ph)
    if not claimed:
        # Someone else claimed this key first - replay their notification.
        existing_row = None
        for _ in range(30):
            existing_row = get_storage().find_idempotency_key_row(idem_key)
            if existing_row:
                break
            _time.sleep(0.02)
        if existing_row:
            nid = existing_row.get("notification_id")
            existing = get_message_summary(nid)
            if existing:
                store_redis(idem_key, nid)
                logger.warning("duplicate notification detected idempotency_key=%s request_id=%s",
                               idem_key, request_id)
                record_audit(
                    user_id=key_id, action="duplicate_notification_attempted",
                    notification_id=nid, status=existing["status"],
                    request_id=request_id, result="already_exists",
                )
                raise HTTPException(
                    status_code=202,
                    headers={"X-Idempotent-Replay": "true"},
                    detail=SendResponse(
                        message_id=existing["message_id"],
                        reference=existing.get("reference"),
                        status=existing["status"],
                        channels=[ChannelQueued(
                            message_id=existing["message_id"],
                            channel=existing["channel"],
                            status=existing["status"],
                            contact=existing["contact"],
                        )],
                    ).model_dump(),
                )
        # Key claimed elsewhere but notification truly not found (edge case).
        logger.warning("idempotency key claimed but no notification found key=%s request_id=%s",
                       idem_key, request_id)
        record_audit(
            user_id=key_id, action="duplicate_notification_attempted",
            notification_id=pre_ids[0], status="processing",
            request_id=request_id, result="already_exists",
        )
        raise HTTPException(
            status_code=202,
            headers={"X-Idempotent-Replay": "true"},
            detail=SendResponse(
                message_id=pre_ids[0],
                status="processing",
                channels=[ChannelQueued(
                    message_id=pre_ids[0],
                    channel=payload.channels[0].channel.value if payload.channels else "sms",
                    status="processing",
                    contact=payload.channels[0].contact if payload.channels else "",
                )],
            ).model_dump(),
        )

    # ---- Enqueue ----
    summary = orchestrate_send(payload, background_tasks, message_ids=pre_ids)
    logger.debug("send database and dispatch complete request_id=%s user_id=%s notification_id=%s status=queued",
                 request_id, user_id, summary["message_id"])

    # Map the idempotency key to the first channel's message_id so a future
    # duplicate request can replay the original result via Redis.
    first_mid = summary["channels"][0]["message_id"]
    store_redis(idem_key, first_mid)

    logger.info("API request completed request_id=%s group_id=%s status=queued",
                request_id, summary["message_id"])
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
        "`deliveries` list targets each channel with its own payload."
    ),
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def send_event(payload: NotificationEventRequest, background_tasks: BackgroundTasks,
               request: Request) -> SendResponse:
    request_id = _get_request_id(request)
    user_id = user_id_from_request(request)
    logger.info("API request received method=event event_type=%s request_id=%s user_id=%s",
                payload.event_type, request_id, user_id)
    payload._request_id = request_id
    payload._user_id = user_id

    for delivery in payload.deliveries:
        try:
            validate_contact(delivery.channel, delivery.payload.recipient)
        except ContactValidationError as exc:
            raise _send_error("validation_error", str(exc), field="deliveries") from exc

    key_id = request.headers.get("X-API-Key", "anon")[:16]
    rl = check_api_send(key_id)
    if not rl.allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(rl.reset_seconds)},
                            detail=ErrorResponse(error={"code": "rate_limited",
                                                          "message": "Limit exceeded.",
                                                          "field": None}).model_dump())

    summary = orchestrate_event(payload, background_tasks)
    logger.info("API request completed request_id=%s group_id=%s status=queued",
                request_id, summary["message_id"])
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
)
def status(notification_id: str, request: Request) -> StatusResponse:
    request_id = _get_request_id(request)
    user_id = user_id_from_request(request)
    logger.info("API request received method=status notification_id=%s request_id=%s user_id=%s",
                notification_id, request_id, user_id)
    logger.debug("status lookup started request_id=%s notification_id=%s user_id=%s",
                 request_id, notification_id, user_id)

    # Prefer the group view; fall back to a single message id.
    group = get_group_summary(notification_id)
    if group is not None:
        logger.info("API request completed request_id=%s notification_id=%s status=%s",
                    request_id, notification_id, group["status"])
        record_audit(
            user_id=user_id, action="notification_status_queried",
            notification_id=group["message_id"], status=group["status"],
            request_id=request_id, result="success",
        )
        return StatusResponse(
            success=True,
            message_id=group["message_id"],
            reference=group.get("reference"),
            status=group["status"],
            channels=[ChannelStatus(**c) for c in group["channels"]],
        )

    single = get_message_summary(notification_id)
    if single is not None:
        logger.info("API request completed request_id=%s message_id=%s status=%s",
                    request_id, notification_id, single["status"])
        record_audit(
            user_id=user_id, action="notification_status_queried",
            notification_id=single["message_id"], channel=single["channel"],
            status=single["status"], request_id=request_id, result="success",
        )
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
                    provider=single.get("provider"),
                    provider_message_id=single.get("provider_message_id"),
                    error=single.get("error"),
                    created_at=single["created_at"],
                    updated_at=single["updated_at"],
                    elapsed_seconds=single.get("elapsed_seconds"),
                    timed_out=single.get("timed_out", False),
                    delivery_timeout_seconds=single.get("delivery_timeout_seconds"),
                    read_at=single.get("read_at"),
                    acknowledged_at=single.get("acknowledged_at"),
                    acknowledgement_type=single.get("acknowledgement_type"),
                )
            ],
        )

    logger.warning("API request completed request_id=%s notification_id=%s result=not_found",
                   request_id, notification_id)
    record_audit(
        user_id=user_id, action="notification_status_queried",
        notification_id=notification_id, request_id=request_id,
        result="failure", failure_reason="not_found",
    )
    raise HTTPException(
        status_code=404,
        detail=ErrorResponse(
            error={"code": "not_found", "message": f"No notification found with id '{notification_id}'"}
        ).model_dump(),
    )
