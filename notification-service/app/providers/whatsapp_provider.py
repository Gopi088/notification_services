"""
WhatsApp provider backed by Twilio's WhatsApp Messaging API.

Docs: https://www.twilio.com/docs/whatsapp/api
Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM
(a Twilio-enabled WhatsApp sender, e.g. "whatsapp:+14155238886").
"""
import uuid

import httpx

from app.config import get_settings
from app.providers.base import NotificationProvider, ProviderConfigError, ProviderError, ProviderResult

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class WhatsAppProvider(NotificationProvider):
    name = "twilio_whatsapp"

    def __init__(self):
        self.settings = get_settings()

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        s = self.settings
        if not (s.TWILIO_ACCOUNT_SID and s.TWILIO_AUTH_TOKEN and s.TWILIO_WHATSAPP_FROM):
            raise ProviderConfigError(
                "WhatsApp provider is not configured. Set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM in .env."
            )

        to = contact if contact.startswith("whatsapp:") else f"whatsapp:{contact}"
        url = f"{TWILIO_API_BASE}/Accounts/{s.TWILIO_ACCOUNT_SID}/Messages.json"

        try:
            resp = httpx.post(
                url,
                auth=(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN),
                data={"From": s.TWILIO_WHATSAPP_FROM, "To": to, "Body": message},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Network error contacting Twilio: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"Twilio WhatsApp API error ({resp.status_code}): {resp.text}")

        data = resp.json()
        return ProviderResult(self.name, data.get("sid", ""), "sent")
