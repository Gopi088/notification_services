import re
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.schemas import Channel

_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")  # loose E.164-style check
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactValidationError(ValueError):
    pass


def validate_contact(channel: Channel, contact: str) -> None:
    contact = contact.strip()
    if channel in (Channel.whatsapp, Channel.sms):
        digits_only = contact.replace(" ", "").replace("-", "")
        if not _PHONE_RE.match(digits_only):
            raise ContactValidationError(
                f"'{contact}' is not a valid phone number for {channel.value}. "
                "Use E.164 format, e.g. +14155551234."
            )
    elif channel == Channel.email:
        if not _EMAIL_RE.match(contact):
            raise ContactValidationError(f"'{contact}' is not a valid email address.")


# Per-channel message length limits (configurable). Enforced BEFORE sending;
# exceeding them returns HTTP 413 with a clear error.
def _channel_message_limit(channel: str) -> int:
    s = get_settings()
    limits = {
        "sms": s.SMS_MAX_MESSAGE_LENGTH,
        "whatsapp": s.WHATSAPP_MAX_MESSAGE_LENGTH,
        "email": s.EMAIL_MAX_MESSAGE_LENGTH,
    }
    return limits.get(channel, 0)


def validate_message_limits(message: str, channels: List[Any]) -> Optional[str]:
    """Return an error string if the message exceeds a channel limit, else None."""
    msg_len = len(message or "")
    for cr in channels:
        ch = getattr(cr, "channel", cr)
        ch_name = ch.value if hasattr(ch, "value") else str(ch)
        limit = _channel_message_limit(ch_name)
        if limit and msg_len > limit:
            return (
                f"message length {msg_len} exceeds the {ch_name} limit of "
                f"{limit} characters"
            )
    return None


def validate_request_size(payload: Any) -> Optional[str]:
    """Return an error string if the serialized request exceeds the total size
    limit, else None."""
    import json

    body = json.dumps(payload.model_dump(exclude_none=True), default=str).encode()
    limit = get_settings().MAX_REQUEST_SIZE_BYTES
    if len(body) > limit:
        return f"request size {len(body)} bytes exceeds the limit of {limit} bytes"
    return None


def validate_attachment_limits(attachments: Optional[List[Any]]) -> Optional[str]:
    """Return an error string if an email attachment exceeds file-size or
    page-count limits, else None."""
    import base64

    s = get_settings()
    for att in attachments or []:
        name = getattr(att, "name", "?") or "?"
        data = getattr(att, "content_base64", None)
        if data:
            try:
                size = len(base64.b64decode(data))
            except Exception:  # noqa: BLE001
                size = len(data)
            if size > s.MAX_FILE_SIZE_BYTES:
                return (
                    f"attachment '{name}' size {size} bytes exceeds the limit of "
                    f"{s.MAX_FILE_SIZE_BYTES} bytes"
                )
        pages = getattr(att, "pages", None)
        if pages is not None and pages > s.MAX_DOCUMENT_PAGES:
            return (
                f"attachment '{name}' has {pages} pages, exceeding the limit of "
                f"{s.MAX_DOCUMENT_PAGES}"
            )
    return None
