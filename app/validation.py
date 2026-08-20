import re

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
