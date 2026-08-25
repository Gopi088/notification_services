"""
Versioned (v1) API schemas.

The public contract is stable: request/response shapes are decoupled from the
internal provider/model layer, so internals are never leaked to callers.
"""
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class Channel(str, Enum):
    whatsapp = "whatsapp"
    sms = "sms"
    email = "email"


# Internally we may add channels (telegram, push, ...) without touching the
# public v1 enum above. Channels are validated at the API layer.
class Status(str, Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    partial = "partial"


class TemplateParam(BaseModel):
    """External template parameter, e.g. the body placeholder value."""

    name: str = Field(..., description="Parameter name referenced by the template, e.g. 'body'")
    value: str = Field(..., description="Value substituted into the template")


class ChannelRequest(BaseModel):
    """One medium (channel) within a send request."""

    channel: Channel
    contact: str = Field(
        ...,
        min_length=3,
        max_length=254,
        description="Phone number (E.164-ish) for whatsapp/sms, email address for email",
    )
    # External template support: if provided, the channel renders the message
    # through that template instead of free text.
    template_name: Optional[str] = Field(
        None, description="Approved external template name (WhatsApp: Meta template)"
    )
    template_language: Optional[str] = Field(
        None, description="Template language code, e.g. 'en' or 'en_US'"
    )
    template_params: Optional[List[TemplateParam]] = Field(
        None, description="Template parameter values to substitute"
    )

    @field_validator("contact")
    @classmethod
    def strip_contact(cls, v: str) -> str:
        return v.strip()


class SendRequest(BaseModel):
    """A send may target one or more channels; each channel carries its own contact/template."""

    channels: List[ChannelRequest] = Field(
        ..., min_length=1, description="One or more channels to deliver through"
    )
    message: str = Field(..., min_length=1, max_length=4096)
    reference: Optional[str] = Field(
        None, max_length=128, description="Caller-supplied reference, e.g. an order id"
    )

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        return v

    @field_validator("channels")
    @classmethod
    def reject_duplicate_channels(cls, v: List[ChannelRequest]) -> List[ChannelRequest]:
        seen = set()
        for c in v:
            if c.channel in seen:
                raise ValueError(f"channel '{c.channel.value}' specified more than once")
            seen.add(c.channel)
        return v


class ChannelQueued(BaseModel):
    message_id: str
    channel: str
    status: Status
    contact: str


class SendResponse(BaseModel):
    """Accepted send. `message_id` is the group id; per-channel ids are in `channels`."""

    success: bool = True
    message_id: str
    reference: Optional[str] = None
    status: Status = Status.queued
    channels: List[ChannelQueued]


class ChannelStatus(BaseModel):
    message_id: str
    channel: str
    contact: str
    status: Status
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    attempt_count: int = 0
    # Seconds elapsed since the message was created (how long to wait so far).
    elapsed_seconds: Optional[float] = None
    # True when still queued/sent and elapsed_seconds > delivery_timeout_seconds.
    timed_out: bool = False
    delivery_timeout_seconds: Optional[int] = None


class StatusResponse(BaseModel):
    success: bool = True
    message_id: str
    reference: Optional[str] = None
    status: Status
    channels: List[ChannelStatus]


# ---------------------------------------------------------------------------
# Event-driven sends (deliveries format)
# ---------------------------------------------------------------------------


class WhatsAppTemplate(BaseModel):
    """External WhatsApp (Meta) template reference."""

    id: str = Field(..., min_length=1, description="Approved Meta/WhatsApp template name")
    language: Optional[str] = Field(None, description="Template language code, e.g. 'en' or 'en_US'")
    params: Optional[List[TemplateParam]] = Field(
        None, description="Template parameter values to substitute"
    )


class WhatsAppPayload(BaseModel):
    """Payload for a whatsapp delivery."""

    recipient: str = Field(..., min_length=3, max_length=254)
    message: Optional[str] = Field(
        None, description="Free-form text (only works inside a 24h session window)"
    )
    template: Optional[WhatsAppTemplate] = Field(
        None, description="Approved Meta template (required to reach a new contact)"
    )


class SMSPayload(BaseModel):
    """Payload for an sms delivery."""

    recipient: str = Field(..., min_length=3, max_length=254)
    message: str = Field(..., min_length=1, max_length=4096)


class EmailAttachment(BaseModel):
    """One attachment on an email delivery."""

    name: str = Field(..., min_length=1, max_length=255, description="File name, e.g. Interview_Guide.pdf")
    url: Optional[str] = Field(
        None, max_length=2048, description="Public HTTPS URL to download the file from"
    )
    type: Optional[str] = Field(None, max_length=128, description="MIME type, e.g. application/pdf")
    content_base64: Optional[str] = Field(
        None,
        max_length=30000000,
        description="Base64-encoded file content (alternative to url; max 30 MB of encoded data)",
    )


class EmailPayload(BaseModel):
    """Payload for an email delivery."""

    recipient: str = Field(..., min_length=3, max_length=254)
    subject: Optional[str] = Field("Notification", max_length=200)
    message: Optional[str] = Field(None, max_length=4096, description="Plain-text body")
    html: Optional[str] = Field(None, description="HTML body (overrides template rendering)")
    cc: Optional[List[str]] = Field(None, description="CC recipients")
    bcc: Optional[List[str]] = Field(None, description="BCC recipients")
    replyTo: Optional[str] = Field(
        None, alias="reply_to", max_length=254, description="Reply-To address"
    )
    attachments: Optional[List[EmailAttachment]] = Field(
        None, max_length=10, description="Files to attach (downloaded from url or sent as content_base64)"
    )

    model_config = {"populate_by_name": True}


class DeliveryRequest(BaseModel):
    """One delivery: a channel plus its own payload, validated per channel."""

    channel: Channel
    payload: Union[WhatsAppPayload, SMSPayload, EmailPayload] = Field(
        ..., description="Channel-specific payload"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_payload(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        channel = values.get("channel")
        payload = values.get("payload")
        if channel in (Channel.whatsapp, "whatsapp") and isinstance(payload, dict):
            values["payload"] = WhatsAppPayload(**payload)
        elif channel in (Channel.sms, "sms") and isinstance(payload, dict):
            values["payload"] = SMSPayload(**payload)
        elif channel in (Channel.email, "email") and isinstance(payload, dict):
            values["payload"] = EmailPayload(**payload)
        return values


class NotificationEventRequest(BaseModel):
    """
    Event-driven send: one envelope, one delivery per channel.

    Example: an interview_confirmation event fanning out to whatsapp + sms +
    email, each delivery carrying its own recipient and channel-specific fields.
    """

    request_id: Optional[str] = Field(None, max_length=128, description="Caller-supplied request id")
    event_type: Optional[str] = Field(None, max_length=64, description="Event that triggered the send")
    ref: Optional[str] = Field(None, max_length=128, description="Caller-supplied reference")
    data: Optional[Any] = Field(
        None,
        description=(
            "Event data. A string is used as a fallback message body; a dict is "
            "used as fallback WhatsApp template params."
        ),
    )
    deliveries: List[DeliveryRequest] = Field(
        ..., min_length=1, description="One delivery per channel"
    )


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: Optional[str] = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail


class HealthResponse(BaseModel):
    success: bool = True
    service: str
    version: str
    mock_mode: bool
