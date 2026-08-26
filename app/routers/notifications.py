"""
Legacy router (v0) kept for backward compatibility.

New callers should use /api/v1. These unversioned routes still work but map
onto the same orchestrator so behavior stays consistent.
"""
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.auth import user_id_from_request
from app.audit import record_audit
from app.logging_config import new_request_id
from app.orchestrator import get_message_summary, orchestrate_send
from app.schemas import Channel, SendRequest as V1SendRequest, ChannelRequest
from app.storage import get_storage
from app.validation import ContactValidationError, validate_contact

router = APIRouter()
logger = logging.getLogger("api.legacy")


class LegacySendRequest(BaseModel):
    channel: Channel
    contact: str = Field(..., min_length=3, max_length=254)
    message: str = Field(..., min_length=1, max_length=4096)

    @field_validator("contact")
    @classmethod
    def strip_contact(cls, v: str) -> str:
        return v.strip()

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        return v


class LegacyError(BaseModel):
    detail: str


@router.post(
    "/send",
    status_code=202,
    summary="Queue a message for delivery (legacy single-channel API)",
    responses={400: {"model": LegacyError}},
)
def send_message(payload: LegacySendRequest, background_tasks: BackgroundTasks, request: Request) -> dict:
    request_id = new_request_id(request.headers.get("X-Request-ID"))
    user_id = user_id_from_request(request)
    logger.debug("legacy send request parsed request_id=%s user_id=%s channel=%s",
                 request_id, user_id, payload.channel.value)
    try:
        validate_contact(payload.channel, payload.contact)
    except ContactValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    request_payload = V1SendRequest(
        channels=[ChannelRequest(channel=payload.channel, contact=payload.contact)],
        message=payload.message,
    )
    request_payload._request_id = request_id
    request_payload._user_id = user_id
    summary = orchestrate_send(request_payload, background_tasks)
    first = summary["channels"][0]
    logger.debug("legacy send response request_id=%s user_id=%s notification_id=%s channel=%s status=queued",
                 request_id, user_id, first["message_id"], first["channel"])
    return {"message_id": first["message_id"], "status": "queued"}


@router.get(
    "/status/{message_id}",
    summary="Get delivery status (legacy API)",
    responses={404: {"model": LegacyError}},
)
def get_status(message_id: str, request: Request) -> dict:
    request_id = new_request_id(request.headers.get("X-Request-ID"))
    user_id = user_id_from_request(request)
    logger.info("API request received method=status notification_id=%s request_id=%s user_id=%s",
                message_id, request_id, user_id)

    row = get_message_summary(message_id)
    if row is None:
        logger.warning("API request completed request_id=%s notification_id=%s result=not_found",
                       request_id, message_id)
        record_audit(
            user_id=user_id, action="notification_status_queried",
            notification_id=message_id, request_id=request_id,
            result="failure", failure_reason="not_found",
        )
        raise HTTPException(status_code=404, detail=f"No message found with id '{message_id}'")

    logger.info("API request completed request_id=%s notification_id=%s status=%s",
                request_id, message_id, row["status"])
    record_audit(
        user_id=user_id, action="notification_status_queried",
        notification_id=row["message_id"], channel=row["channel"],
        status=row["status"], request_id=request_id, result="success",
    )
    return {
        "message_id": row["message_id"],
        "channel": row["channel"],
        "contact": row["contact"],
        "status": row["status"],
        "provider": row.get("provider"),
        "provider_message_id": row.get("provider_message_id"),
        "error": row.get("error"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
