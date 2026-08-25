import re

from app.schemas import Channel

_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")  # loose E.164-style check
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactValidationError(ValueError):
    pass


def validate_contact(channel: Channel, contact: str) -> None:
    contact = contact.strip()
    if channel in (Channel.whatsapp, Channel.sms):
        if not contact:
            raise ContactValidationError(
                f"Phone number cannot be empty for {channel.value}."
            )
        if not _PHONE_RE.match(contact):
            raise ContactValidationError(
                f"'{contact}' is not a valid phone number for {channel.value}. "
                "Use E.164 format, e.g. +14155551234 (digits only, no spaces or dashes)."
            )
    elif channel == Channel.email:
        if not contact:
            raise ContactValidationError("Email address cannot be empty.")
        if not _EMAIL_RE.match(contact):
            raise ContactValidationError(f"'{contact}' is not a valid email address.")
