"""
SMS provider backed by Twilio's Programmable Messaging API.

Docs: https://www.twilio.com/docs/sms
Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_SMS_FROM
(a Twilio phone number, e.g. "+14155551234").
"""
import uuid

import httpx

from app.config import get_settings
from app.providers.base import NotificationProvider, ProviderConfigError, ProviderError, ProviderResult

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class SMSProvider(NotificationProvider):
    name = "twilio_sms"

    def __init__(self):
        self.settings = get_settings()

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        s = self.settings
        if not (s.TWILIO_ACCOUNT_SID and s.TWILIO_AUTH_TOKEN and s.TWILIO_SMS_FROM):
            raise ProviderConfigError(
                "SMS provider is not configured. Set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN and TWILIO_SMS_FROM in .env."
            )

        url = f"{TWILIO_API_BASE}/Accounts/{s.TWILIO_ACCOUNT_SID}/Messages.json"

        try:
            resp = httpx.post(
                url,
                auth=(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN),
                data={"From": s.TWILIO_SMS_FROM, "To": contact, "Body": message},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Network error contacting Twilio: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"Twilio SMS API error ({resp.status_code}): {resp.text}")

        data = resp.json()
        return ProviderResult(self.name, data.get("sid", ""), "sent")
