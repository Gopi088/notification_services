from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Channel(str, Enum):
    whatsapp = "whatsapp"
    sms = "sms"
    email = "email"


class MessageStatus(str, Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"


class SendRequest(BaseModel):
    channel: Channel
    contact: str = Field(..., min_length=3, max_length=254, description="Phone number (E.164) or email address")
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


class SendResponse(BaseModel):
    message_id: str
    status: MessageStatus


class StatusResponse(BaseModel):
    message_id: str
    channel: Channel
    contact: str
    status: MessageStatus
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


class ErrorResponse(BaseModel):
    detail: str
