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
import datetime
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app import __version__
from app.auth import user_id_from_request
from app.config import get_settings
from app.idempotency import (
    check_redis,
    content_fingerprint,
    derive_key,
    normalize_client_key,
    payload_hash,
)
from app.metrics import timed
from app.logging_config import new_request_id
from app.orchestrator import (
    get_group_summary,
    get_message_summary,
    orchestrate_event,
    orchestrate_send,
)
from app.ratelimit import check_api_send, check_recipient
from app.schemas import (
    CandidateMessage,
    CandidateReport,
    ChannelStatus,
    ChannelQueued,
    ErrorResponse,
    NotificationEventRequest,
    SendRequest,
    SendResponse,
    StatusResponse,
)
from app.audit import record_audit
from app.storage import get_storage
from app.validation import (
    ContactValidationError,
    validate_attachment_limits,
    validate_contact,
    validate_message_limits,
    validate_request_size,
)

logger = logging.getLogger("api.v1")

router = APIRouter(prefix="/api/v1", tags=["v1"])

_settings = get_settings()


def _send_error(code: str, message: str, field: Optional[str] = None) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=ErrorResponse(error={"code": code, "message": message, "field": field}).model_dump(),
    )


def _payload_too_large(detail: str) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=ErrorResponse(error={"code": "payload_too_large", "message": detail, "field": None}).model_dump(),
    )


DUPLICATE_MESSAGE = "Message already sent recently. Resend?"


def _duplicate_send_response(
    request_id: str, user_id: str, original: dict, channel: Optional[str] = None,
) -> SendResponse:
    """Build the 202 duplicate response (existing result, NOT resent) + audit.

    The original notification is left untouched and its existing message_id /
    status are returned so the caller can decide whether to resend.
    """
    status = original.get("status", "queued")
    chan = channel or original.get("channel")
    record_audit(
        user_id=user_id, action="duplicate_attempted",
        notification_id=original["message_id"], channel=chan,
        status=status, request_id=request_id, result="duplicate",
    )
    return SendResponse(
        message_id=original["message_id"],
        reference=original.get("reference"),
        status=status,
        duplicate=True,
        message=DUPLICATE_MESSAGE,
        channels=[ChannelQueued(
            message_id=original["message_id"],
            channel=chan or "sms",
            status=status,
            contact=original.get("contact", ""),
        )],
    )


def _within_window(created_at: Optional[str], window_minutes: int) -> bool:
    """True when a notification's created_at falls within the duplicate window.

    The window is compared against the notification's creation time (not the
    idempotency key's creation time) so that a notification sent outside the
    window is treated as a new send regardless of key expiry.
    """
    if not window_minutes or window_minutes <= 0:
        return False  # window disabled
    if not created_at:
        return True  # unknown age — err on the safe side
    try:
        created = datetime.datetime.fromisoformat(created_at)
        age = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()
        return age <= window_minutes * 60
    except Exception:
        return True


def _is_duplicate(original: dict, has_client_key: bool, window_minutes: int) -> bool:
    """True when the request should be treated as a duplicate.

    Client-supplied Idempotency-Keys always replay (explicit contract).
    Server-derived keys replay only when the original notification is still
    within the duplicate window.
    """
    return bool(has_client_key) or _within_window(original.get("created_at"), window_minutes)


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
@timed("request_send_total")
def send(payload: SendRequest, background_tasks: BackgroundTasks, request: Request,
         response: Response) -> SendResponse:
    request_id = _get_request_id(request)
    user_id = user_id_from_request(request)
    logger.info("API request received method=send channels=%s request_id=%s user_id=%s",
                ",".join(c.channel.value for c in payload.channels), request_id, user_id)
    payload._request_id = request_id
    payload._user_id = user_id
    logger.debug("send request parsed request_id=%s user_id=%s channel_count=%d",
                 request_id, user_id, len(payload.channels))

    # ---- Payload limits (validate BEFORE processing/sending) ----
    limit_err = (
        validate_message_limits(payload.message, payload.channels)
        or validate_request_size(payload)
    )
    if limit_err:
        logger.warning("payload limit exceeded request_id=%s user_id=%s detail=%s",
                       request_id, user_id, limit_err)
        record_audit(
            user_id=user_id, action="payload_limit_exceeded",
            result="failure", failure_reason=limit_err, request_id=request_id,
        )
        raise _payload_too_large(limit_err)

    key_id = user_id  # rate-limit bucket keyed on the authenticated identity

    # ---- Rate limiting ----
    rl = check_api_send(key_id)
    logger.debug("send rate limit checked request_id=%s user_id=%s allowed=%s remaining=%d",
                 request_id, user_id, rl.allowed, rl.remaining)
    if not rl.allowed:
        # Log the hashed user identity, never the raw API key.
        logger.warning("rate limited send user_id=%s request_id=%s", user_id, request_id)
        record_audit(
            user_id=user_id, action="rate_limit_exceeded", result="failure",
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
            user_id=user_id,
        )
    logger.debug("send idempotency resolved request_id=%s user_id=%s source=%s",
                 request_id, user_id, "client" if header_key else "derived")

    # Server-derived keys expire after the duplicate window so the same content
    # can be sent again once the window has passed (a duplicate outside the
    # window is a NEW notification). Client-supplied keys keep their own TTL.
    duplicate_window = get_settings().DUPLICATE_WINDOW_MINUTES
    derived_ttl = None
    if not header_key and duplicate_window and duplicate_window > 0:
        derived_ttl = duplicate_window * 60

    # Redis fast-path duplicate check.
    existing_id = check_redis(idem_key)
    if existing_id and not payload.resend:
        existing = get_message_summary(existing_id)
        if existing and _is_duplicate(existing, header_key, duplicate_window):
            response.headers["X-Idempotent-Replay"] = "true"
            return _duplicate_send_response(request_id, user_id, existing)
        if existing:
            # Derived key but the original is outside the window - send anew.
            logger.info("duplicate outside window, sending as new notification request_id=%s",
                        request_id)
            idem_key = f"{idem_key}:{int(time.time())}"

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

    # ---- Window-based content duplicate check ----
    # A notification with the same user + channel + recipient + message/template
    # content sent within DUPLICATE_WINDOW_MINUTES is a duplicate. Outside the
    # window it is treated as a new send. Explicit resends always proceed.
    window = get_settings().DUPLICATE_WINDOW_MINUTES
    if window and window > 0 and not payload.resend:
        storage = get_storage()
        for cr in payload.channels:
            params = {p.name: p.value for p in cr.template_params} if cr.template_params else None
            chash = content_fingerprint(
                user_id, cr.channel.value, cr.contact, payload.message,
                cr.template_name, params,
            )
            dup = storage.find_recent_by_content_hash(chash, window)
            if dup:
                original = get_message_summary(dup["message_id"])
                if original:
                    logger.warning(
                        "duplicate notification within window detected request_id=%s user_id=%s channel=%s",
                        request_id, user_id, cr.channel.value,
                    )
                    response.headers["X-Idempotent-Replay"] = "true"
                    return _duplicate_send_response(request_id, user_id, original, channel=cr.channel.value)

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
        if _is_duplicate(original, header_key, duplicate_window):
            # Accidental duplicate: return the existing notification, DO NOT resend.
            store_redis(idem_key, original_nid, ex=derived_ttl)
            response.headers["X-Idempotent-Replay"] = "true"
            return _duplicate_send_response(request_id, user_id, original)
        # Derived key but the original is outside the window: this content is
        # a NEW notification. Use a fresh key so the old row cannot dedupe it.
        logger.info("duplicate outside window, sending as new notification request_id=%s",
                    request_id)
        idem_key = f"{idem_key}:{int(time.time())}"
        existing_row = None
        original = None

    if existing_row and original and payload.resend:
        # Explicit resend: create a NEW notification linked to the original.
        # Each resend uses a fresh idempotency key so it cannot be accidentally
        # deduplicated against the original or another resend.
        resend_key = f"{idem_key}:resend:{uuid.uuid4().hex[:12]}"
        channel = payload.channels[0].channel.value if payload.channels else None
        logger.warning("resend requested idempotency_key=%s original=%s request_id=%s",
                       idem_key, original_nid, request_id)
        record_audit(
            user_id=user_id, action="duplicate_attempted",
            notification_id=original_nid, channel=channel,
            status=original["status"], request_id=request_id,
            result="duplicate", metadata={"resend": True},
        )

        # Link the resend to the original's internal id (parent_notification_id
        # is a foreign key onto notifications.id, which may differ from the
        # public message_id recorded in the idempotency keys table).
        original_row = get_storage().get_notification_by_message_id(original_nid)
        parent_id = original_row["id"] if original_row else original_nid

        resend_ids = [str(uuid.uuid4()) for _ in payload.channels]
        claim_idempotency_key(resend_key, resend_ids[0], ph, ttl_seconds=derived_ttl)
        summary = orchestrate_send(payload, background_tasks, message_ids=resend_ids,
                                   parent_notification_id=parent_id)
        store_redis(resend_key, resend_ids[0], ex=derived_ttl)
        new_mid = summary["channels"][0]["message_id"]
        record_audit(
            user_id=user_id, action="resend",
            notification_id=new_mid, channel=channel,
            request_id=request_id,
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
    claimed = claim_idempotency_key(idem_key, pre_ids[0], ph, ttl_seconds=derived_ttl)
    if not claimed:
        # Someone else claimed this key first - replay their notification.
        existing_row = None
        for _ in range(30):
            existing_row = get_storage().find_idempotency_key_row(idem_key)
            if existing_row:
                break
            _time.sleep(0.02)
        existing = None
        if existing_row:
            nid = existing_row.get("notification_id")
            existing = get_message_summary(nid)
            if existing and _is_duplicate(existing, header_key, duplicate_window):
                store_redis(idem_key, nid, ex=derived_ttl)
                response.headers["X-Idempotent-Replay"] = "true"
                return _duplicate_send_response(request_id, user_id, existing)
            if existing:
                # Contended key but notification outside the window: retry with
                # a fresh derived key so the send is not lost.
                logger.info("duplicate outside window, retrying with fresh key request_id=%s",
                            request_id)
                idem_key = f"{idem_key}:{int(time.time())}"
                claimed = claim_idempotency_key(idem_key, pre_ids[0], ph, ttl_seconds=derived_ttl)
                if claimed:
                    existing_row = None
                    existing = None
                else:
                    # Still contended — replay the existing result.
                    store_redis(idem_key, nid, ex=derived_ttl)
                    response.headers["X-Idempotent-Replay"] = "true"
                    return _duplicate_send_response(request_id, user_id, existing)
        if existing_row is not None or existing is not None:
            # Key claimed elsewhere but notification truly not found (edge case).
            logger.warning("idempotency key claimed but no notification found key=%s request_id=%s",
                           idem_key, request_id)
            record_audit(
                user_id=user_id, action="duplicate_attempted",
                notification_id=pre_ids[0], status="processing",
                request_id=request_id, result="duplicate",
            )
            response.headers["X-Idempotent-Replay"] = "true"
            return SendResponse(
                message_id=pre_ids[0],
                status="processing",
                duplicate=True,
                message=DUPLICATE_MESSAGE,
                channels=[ChannelQueued(
                    message_id=pre_ids[0],
                    channel=payload.channels[0].channel.value if payload.channels else "sms",
                    status="processing",
                    contact=payload.channels[0].contact if payload.channels else "",
                )],
            )

    # ---- Enqueue ----
    summary = orchestrate_send(payload, background_tasks, message_ids=pre_ids)
    logger.debug("send database and dispatch complete request_id=%s user_id=%s notification_id=%s status=queued",
                 request_id, user_id, summary["message_id"])

    # Map the idempotency key to the first channel's message_id so a future
    # duplicate request can replay the original result via Redis.
    first_mid = summary["channels"][0]["message_id"]
    store_redis(idem_key, first_mid, ex=derived_ttl)

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

    # ---- Payload limits (validate BEFORE processing/sending) ----
    for delivery in payload.deliveries:
        limit_err = (
            validate_message_limits(
                getattr(delivery.payload, "message", "") or "", [delivery.channel]
            )
            or validate_request_size(payload)
            or validate_attachment_limits(
                getattr(delivery.payload, "attachments", None)
            )
        )
        if limit_err:
            logger.warning("payload limit exceeded request_id=%s user_id=%s detail=%s",
                           request_id, user_id, limit_err)
            record_audit(
                user_id=user_id, action="payload_limit_exceeded",
                result="failure", failure_reason=limit_err, request_id=request_id,
            )
            raise _payload_too_large(limit_err)

    key_id = user_id  # rate-limit bucket keyed on the authenticated identity
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

    # On-demand delivery poll: when the message is submitted/sent and the
    # provider supports it, query the real delivery state so the status shows
    # delivered/failed even without a webhook. Best-effort (never fails).
    try:
        from app.orchestrator import poll_delivery_status

        poll_delivery_status(notification_id)
    except Exception:  # noqa: BLE001 - polling must never break a status read
        pass

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
                    retry_count=single.get("retry_count", 0),
                    created_at=single["created_at"],
                    updated_at=single["updated_at"],
                    elapsed_seconds=single.get("elapsed_seconds"),
                    timed_out=single.get("timed_out", False),
                    delivery_timeout_seconds=single.get("delivery_timeout_seconds"),
                    delivered_at=single.get("delivered_at"),
                    read_at=single.get("read_at"),
                    acknowledged_at=single.get("acknowledged_at"),
                    acknowledgement_type=single.get("acknowledgement_type"),
                    delivery_confirmation=single.get("delivery_confirmation", "unavailable"),
                    history=single.get("history", []),
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


@router.get(
    "/reports/candidates/{candidate_id}",
    response_model=CandidateReport,
    summary="Candidate communication report",
    description=(
        "Returns a delivery report for a candidate/contact: message counts by "
        "channel and status, plus the detailed notification records. When auth "
        "is enabled, only the authenticated user's own messages are included."
    ),
    responses={404: {"model": ErrorResponse}},
)
def candidate_report(candidate_id: str, request: Request,
                     limit: int = 50, offset: int = 0) -> CandidateReport:
    request_id = _get_request_id(request)
    user_id = user_id_from_request(request)
    logger.info("candidate report requested candidate_id=%s request_id=%s user_id=%s",
                candidate_id, request_id, user_id)
    record_audit(
        user_id=user_id, action="candidate_report_queried",
        request_id=request_id, metadata={"candidate_id": candidate_id},
    )

    storage = get_storage()
    # Authorization scope: when authenticated, only the caller's own messages.
    scope = None if user_id == "anonymous" else user_id
    rows = storage.list_notifications_by_recipient(
        recipient=candidate_id, created_by=scope, limit=100000, offset=0,
    )
    if not rows:
        logger.info("candidate report empty candidate_id=%s request_id=%s", candidate_id, request_id)
        return CandidateReport(candidate_id=candidate_id, total_messages=0,
                               by_channel={}, by_status={}, messages=[])

    total = len(rows)
    by_channel: dict = {}
    by_status: dict = {}
    for row in rows:
        by_channel[row["channel"]] = by_channel.get(row["channel"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    safe_limit = min(max(limit, 1), 100)
    safe_offset = max(offset, 0)
    page = rows[safe_offset:safe_offset + safe_limit]
    messages = [
        CandidateMessage(
            message_id=row["message_id"],
            channel=row["channel"],
            contact=row["recipient"],
            status=row["status"],
            provider=row.get("provider"),
            provider_message_id=row.get("provider_message_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            delivered_at=row.get("delivered_at"),
            read_at=row.get("read_at"),
            retry_count=row.get("retry_count", 0),
            error=row.get("last_error"),
            group_id=row.get("group_id"),
            reference=row.get("reference"),
            resend_count=row.get("resend_count", 0),
        )
        for row in page
    ]
    logger.info("candidate report completed candidate_id=%s total=%d request_id=%s",
                candidate_id, total, request_id)
    return CandidateReport(
        candidate_id=candidate_id,
        total_messages=total,
        by_channel=by_channel,
        by_status=by_status,
        messages=messages,
    )


@router.get(
    "/performance/metrics",
    response_model=dict,
    summary="Per-process performance metrics (when enabled)",
    description=(
        "Returns aggregated timing metrics (count/p50/p95/max/avg per operation) "
        "collected by this process. Metrics are PER-PROCESS: with multiple "
        "Uvicorn workers each worker reports its own view. Empty object when "
        "PERFORMANCE_METRICS_ENABLED=false."
    ),
)
def performance_metrics() -> dict:
    from app.metrics import snapshot

    return {"process_pid": os.getpid(), "metrics": snapshot()}
