"""
Vonage providers.

SMS provider: used for the SMS channel when VONAGE_API_KEY and
VONAGE_API_SECRET are set in .env (falls back to Azure SMS otherwise, selected
by the provider factory).

WhatsApp provider: used for the WhatsApp channel when VONAGE_WHATSAPP_FROM is
set. It calls the Vonage Messages API Sandbox with the same credentials,
mirroring the working cURL command:

    curl -X POST https://messages-sandbox.nexmo.com/v1/messages \
      -u "$VONAGE_API_KEY:$VONAGE_API_SECRET" \
      -H "Content-Type: application/json" -H "Accept: application/json" \
      -d '{"from": "<from>", "to": "<to>", "message_type": "text",
           "text": "...", "channel": "whatsapp"}'

Vonage expects the `to` number WITHOUT the leading "+" (digits only, 7-15).
"""
import logging
import uuid
from typing import Any, Dict

import requests

from app.config import get_settings
from app.providers.base import (
    NotificationProvider,
    ProviderConfigError,
    ProviderError,
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)
from app.providers.azure_provider import _normalize_phone

logger = logging.getLogger("vonage_provider")


class VonageSMSProvider(NotificationProvider):
    name = "vonage_sms"

    def send(self, contact: str, message: str) -> ProviderResult:
        s = get_settings()
        if s.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        if not s.VONAGE_API_KEY or not s.VONAGE_API_SECRET:
            raise ProviderConfigError(
                "Vonage SMS provider is not configured. Set VONAGE_API_KEY and "
                "VONAGE_API_SECRET in .env (Vonage dashboard -> API Settings)."
            )
        if not s.VONAGE_SMS_FROM:
            raise ProviderConfigError(
                "Vonage SMS provider is not configured. Set VONAGE_SMS_FROM in "
                ".env (your Vonage sender: an E.164 number or approved "
                "alphanumeric sender ID)."
            )

        from vonage import Auth, Vonage
        from vonage_messages import Sms

        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)
        to_digits = to.lstrip("+")

        logger.info(
            "[SMS] Provider: Vonage | From: %s | To: %s",
            s.VONAGE_SMS_FROM, to,
        )

        try:
            client = Vonage(
                Auth(api_key=s.VONAGE_API_KEY, api_secret=s.VONAGE_API_SECRET)
            )
            response = client.messages.send(
                Sms(to=to_digits, from_=s.VONAGE_SMS_FROM, text=message)
            )
        except Exception as exc:
            logger.error("[SMS] Vonage rejected message: %s", exc)
            raise ProviderError(f"Vonage SMS error: {exc}") from exc

        if hasattr(response, "message_uuid"):
            message_id = response.message_uuid
        elif isinstance(response, dict):
            message_id = response.get("message_uuid")
        else:
            message_id = None
        if not message_id:
            raise ProviderError(f"Vonage SMS returned no message id: {response}")

        logger.info("[SMS] Vonage message ID: %s | Provider accepted message", message_id)
        return ProviderResult(self.name, message_id, "sent")

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        return self.send(recipient, message)


class VonageWhatsAppProvider(NotificationProvider):
    name = "vonage_whatsapp"

    def send(self, contact: str, message: str) -> ProviderResult:
        s = get_settings()
        if s.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        if not s.VONAGE_API_KEY or not s.VONAGE_API_SECRET:
            raise ProviderConfigError(
                "Vonage WhatsApp provider is not configured. Set VONAGE_API_KEY "
                "and VONAGE_API_SECRET in .env (Vonage dashboard -> API Settings)."
            )
        if not s.VONAGE_WHATSAPP_FROM:
            raise ProviderConfigError(
                "Vonage WhatsApp provider is not configured. Set VONAGE_WHATSAPP_FROM "
                "in .env (the sandbox 'from' number, e.g. 14157386102)."
            )

        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)
        to_digits = to.lstrip("+")
        sandbox_url = s.VONAGE_WHATSAPP_SANDBOX_URL

        logger.info("[WhatsApp] Provider: Vonage Sandbox")
        logger.info("[WhatsApp] From: %s | To: %s", s.VONAGE_WHATSAPP_FROM, to)

        payload = {
            "from": s.VONAGE_WHATSAPP_FROM,
            "to": to_digits,
            "message_type": "text",
            "text": message,
            "channel": "whatsapp",
        }

        try:
            response = requests.post(
                sandbox_url,
                auth=(s.VONAGE_API_KEY, s.VONAGE_API_SECRET),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
                timeout=30,
            )
        except requests.ConnectionError as exc:
            logger.error("[WhatsApp] Vonage connection failed: %s", exc)
            raise ProviderTransientError(f"Vonage WhatsApp connection error: {exc}") from exc
        except requests.Timeout as exc:
            logger.error("[WhatsApp] Vonage request timed out: %s", exc)
            raise ProviderTransientError(f"Vonage WhatsApp timeout: {exc}") from exc
        except requests.RequestException as exc:
            logger.error("[WhatsApp] Vonage request failed: %s", exc)
            raise ProviderTransientError(f"Vonage WhatsApp network error: {exc}") from exc

        if response.status_code == 401:
            raise ProviderPermanentError(
                "Vonage WhatsApp authentication failed (401). Check VONAGE_API_KEY "
                "and VONAGE_API_SECRET."
            )
        if response.status_code == 403:
            raise ProviderPermanentError(
                "Vonage WhatsApp rejected the request (403). The recipient number "
                "may not be allow-listed in the Vonage Messages Sandbox."
            )
        if response.status_code == 429:
            raise ProviderTransientError(
                f"Vonage WhatsApp rate limited (429): {response.text}"
            )
        if response.status_code >= 500:
            logger.error("[WhatsApp] Vonage server error: %s", response.text)
            raise ProviderTransientError(
                f"Vonage WhatsApp server error ({response.status_code}): {response.text}"
            )
        if response.status_code >= 400:
            logger.error("[WhatsApp] Vonage rejected message: %s", response.text)
            raise ProviderPermanentError(
                f"Vonage WhatsApp error ({response.status_code}): {response.text}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise ProviderError(f"Vonage WhatsApp invalid response: {response.text}") from exc

        message_id = result.get("message_uuid")
        if not message_id:
            raise ProviderError(f"Vonage WhatsApp returned no message id: {result}")

        logger.info("[WhatsApp] Vonage message ID: %s | Provider accepted message", message_id)
        return ProviderResult(self.name, message_id, "sent")

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        return self.send(recipient, message)
