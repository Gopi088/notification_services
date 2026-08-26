"""
Legacy router (v0) kept for backward compatibility.

New callers should use /api/v1. These unversioned routes still work but map
onto the same orchestrator so behavior stays consistent.
"""
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field, field_validator

from app.auth import require_api_key, require_scope
from app.database import get_message
from app.errors import NotFoundError, ValidationError
from app.orchestrator import get_message_summary, orchestrate_send
from app.schemas import Channel, SendRequest as V1SendRequest
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


@router.post(
    "/send",
    status_code=202,
    summary="Queue a message for delivery (legacy single-channel API)",
    dependencies=[Depends(require_api_key), Depends(require_scope("send:write"))],
)
async def send_message(payload: LegacySendRequest, background_tasks: BackgroundTasks) -> dict:
    try:
        validate_contact(payload.channel, payload.contact)
    except ContactValidationError as exc:
        raise ValidationError(str(exc), field="contact") from exc

    summary = await orchestrate_send(
        V1SendRequest(
            channel=payload.channel,
            contact=payload.contact,
            message=payload.message,
        ),
        background_tasks,
    )
    return {"success": True, "message_id": summary["message_id"], "status": "queued"}


@router.get(
    "/status/{message_id}",
    summary="Get delivery status (legacy API)",
    dependencies=[Depends(require_api_key)],
)
def get_status(message_id: str) -> dict:
    row = get_message(message_id)
    if row is None:
        raise NotFoundError(f"No message found with id '{message_id}'")
    return {
        "success": True,
        "message_id": row["message_id"],
        "channel": row["channel"],
        "contact": row["contact"],
        "status": row["status"],
        "provider": row["provider"],
        "provider_message_id": row["provider_message_id"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
