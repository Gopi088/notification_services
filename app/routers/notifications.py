"""
Legacy router (v0) kept for backward compatibility.

New callers should use /api/v1. These unversioned routes still work but map
onto the same orchestrator so behavior stays consistent.
"""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import require_api_key
from app.orchestrator import get_message_summary, orchestrate_send
from app.schemas import Channel, SendRequest as V1SendRequest, ChannelRequest
from app.storage import get_storage
from app.validation import ContactValidationError, validate_contact

router = APIRouter()


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
    dependencies=[Depends(require_api_key)],
)
def send_message(payload: LegacySendRequest, background_tasks: BackgroundTasks) -> dict:
    try:
        validate_contact(payload.channel, payload.contact)
    except ContactValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = orchestrate_send(
        V1SendRequest(
            channels=[ChannelRequest(channel=payload.channel, contact=payload.contact)],
            message=payload.message,
        ),
        background_tasks,
    )
    first = summary["channels"][0]
    return {"message_id": first["message_id"], "status": "queued"}


@router.get(
    "/status/{message_id}",
    summary="Get delivery status (legacy API)",
    responses={404: {"model": LegacyError}},
    dependencies=[Depends(require_api_key)],
)
def get_status(message_id: str) -> dict:
    row = get_message_summary(message_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No message found with id '{message_id}'")
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
