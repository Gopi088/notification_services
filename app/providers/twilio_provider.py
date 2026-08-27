"""
Twilio providers (SMS + WhatsApp) via the Twilio REST Messages API.

Both channels use the same resource, authenticated with HTTP Basic auth
(Account SID : Auth Token):

    POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json

SMS:
    -d "To=+<number>" -d "From=+<number>" -d "Body=<text>"

WhatsApp free-form text (24h session window only):
    -d "To=whatsapp:+<number>" -d "From=whatsapp:+<number>" -d "Body=<text>"

WhatsApp template (reaches NEW numbers, no session required):
    -d "To=whatsapp:+<number>" -d "From=whatsapp:+<number>"
    -d "ContentSid=HX..." [-d 'ContentVariables={"1": "value"}']

Selected by the provider factory when TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
are set in .env (falls back to Vonage/Azure otherwise).
"""
import json
import logging
import uuid
from typing import Any, Dict, Optional

import requests

from app.config import get_settings
from app.providers.azure_provider import _normalize_phone
from app.providers.base import (
    NotificationProvider,
    ProviderConfigError,
    ProviderError,
    ProviderResult,
)

logger = logging.getLogger("twilio_provider")

_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# Error markers in the Twilio response body that mean "free-form text is not
# allowed here - use an approved template instead".
_TRIAL_SMS_ERRORS = ("572006", "predefined SMS templates")
_CONTENTSID_ERRORS = ("21654", "ContentSid Required")


def _error_matches(exc: Exception, needles: tuple) -> bool:
    text = str(exc)
    return any(n in text for n in needles)


def _content_variables(params: Optional[Dict[str, str]]) -> Optional[str]:
    """Build the Twilio ContentVariables JSON string for a content template.

    Twilio placeholder keys are positional strings ("1", "2", ...). When the
    caller supplies named keys they are mapped to their position in insertion
    order so e.g. {"name": "Rahul"} becomes {"1": "Rahul"}.
    """
    if not params:
        return None
    out: Dict[str, str] = {}
    for i, (k, v) in enumerate(params.items(), start=1):
        key = str(k) if str(k).isdigit() else str(i)
        out[key] = str(v)
    return json.dumps(out)


class _TwilioMixin:
    """Shared auth, request and error handling for the Twilio Messages API."""

    def _messages_url(self, sid: str) -> str:
        return _MESSAGES_URL.format(sid=sid)

    def _check_config(self, s) -> None:
        if not s.TWILIO_ACCOUNT_SID or not s.TWILIO_AUTH_TOKEN:
            raise ProviderConfigError(
                "Twilio is not configured. Set TWILIO_ACCOUNT_SID and "
                "TWILIO_AUTH_TOKEN in .env (Twilio console -> Account -> "
                "API keys & tokens)."
            )

    def _post(self, s, data: Dict[str, str]) -> Dict:
        url = self._messages_url(s.TWILIO_ACCOUNT_SID)
        try:
            resp = requests.post(
                url,
                auth=(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN),
                data=data,
                timeout=30,
            )
        except requests.RequestException as exc:  # network / timeout (retryable)
            logger.error("[Twilio] request failed: %s", exc)
            raise ProviderError(
                f"Twilio network error: {exc}", retryable=True, error_code="NETWORK"
            ) from exc

        if resp.status_code in (401, 403):
            raise ProviderError(
                f"Twilio authentication failed ({resp.status_code}). Check "
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
                retryable=False,
                error_code=str(resp.status_code),
            )
        if resp.status_code == 429:
            raise ProviderError(
                f"Twilio rate limited (429): {resp.text}",
                retryable=True,
                error_code="429",
            )
        if resp.status_code >= 500:
            logger.error("[Twilio] rejected message: %s", resp.text)
            raise ProviderError(
                f"Twilio error ({resp.status_code}): {resp.text}",
                retryable=True,
                error_code=str(resp.status_code),
            )
        if resp.status_code >= 400:
            logger.error("[Twilio] rejected message: %s", resp.text)
            raise ProviderError(
                f"Twilio error ({resp.status_code}): {resp.text}",
                retryable=False,
                error_code=str(resp.status_code),
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"Twilio invalid response: {resp.text}",
                retryable=False,
                error_code="BAD_RESPONSE",
            ) from exc

    @staticmethod
    def _result(provider_name: str, data: Dict) -> ProviderResult:
        sid = data.get("sid")
        if not sid:
            raise ProviderError(
                f"Twilio returned no message sid: {data}",
                retryable=False,
                error_code="NO_MESSAGE_ID",
            )
        # Twilio accepts with status "queued"; delivery status is not claimed here.
        return ProviderResult(provider_name, sid, "submitted")


class TwilioSMSProvider(_TwilioMixin, NotificationProvider):
    name = "twilio_sms"

    def send(self, contact: str, message: str) -> ProviderResult:
        s = get_settings()
        if s.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        self._check_config(s)
        if not s.TWILIO_FROM:
            raise ProviderConfigError(
                "Twilio SMS provider is not configured. Set TWILIO_FROM in "
                ".env (your Twilio SMS number, E.164, e.g. +17372508034)."
            )

        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)
        logger.info("[SMS] Provider: Twilio | From: %s | To: %s", s.TWILIO_FROM, to)

        data = {"To": to, "From": s.TWILIO_FROM, "Body": message}
        try:
            result = self._post(s, data)
        except ProviderError as exc:
            # TRIAL accounts reject free-form SMS (572006) and only accept
            # predefined templates. Retry with the configured template so the
            # send succeeds instead of failing.
            if s.TWILIO_SMS_TEMPLATE and _error_matches(exc, _TRIAL_SMS_ERRORS):
                logger.info(
                    "[SMS] Twilio trial account: free-form SMS rejected, falling back to "
                    "predefined template '%s'", s.TWILIO_SMS_TEMPLATE,
                )
                data = {"To": to, "From": s.TWILIO_FROM, "Body": s.TWILIO_SMS_TEMPLATE}
                result = self._post(s, data)
            else:
                raise
        delivery = self._result(self.name, result)
        logger.info("[SMS] Twilio message ID: %s | Provider accepted message", delivery.provider_message_id)
        return delivery

    def send_with_template(
        self,
        contact: str,
        message: str,
        template_name: str,
        template_language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """Send SMS rendered through a local templates/sms/<name>.txt template."""
        from app.message_format import format_sms

        rendered = format_sms(message, template_name, template_params)
        return self.send(contact, rendered)

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        template = payload.get("template")
        if isinstance(template, dict) and template.get("name"):
            params = {p["name"]: p["value"] for p in (template.get("params") or [])}
            return self.send_with_template(recipient, message, template["name"],
                                           template_params=params)
        return self.send(recipient, message)


class TwilioWhatsAppProvider(_TwilioMixin, NotificationProvider):
    name = "twilio_whatsapp"

    def _whatsapp_from(self, s) -> str:
        sender = s.twilio_whatsapp_from
        if not sender:
            raise ProviderConfigError(
                "Twilio WhatsApp provider is not configured. Set "
                "TWILIO_WHATSAPP_FROM or TWILIO_FROM in .env (your "
                "WhatsApp-enabled Twilio number, E.164, e.g. +17372508034)."
            )
        return sender

    def send(self, contact: str, message: str) -> ProviderResult:
        s = get_settings()
        if s.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        self._check_config(s)
        sender = self._whatsapp_from(s)
        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)

        logger.info("[WhatsApp] Provider: Twilio | From: %s | To: %s", sender, to)
        logger.info("[WhatsApp] Message type: TEXT (24h session window)")

        data = {
            "To": f"whatsapp:{to}",
            "From": f"whatsapp:{sender}",
            "Body": message,
        }
        try:
            result = self._post(s, data)
        except ProviderError as exc:
            # Free-form text only works inside a 24h session window. When
            # Twilio requires a template (21654) and a default ContentSid is
            # configured, fall back to the approved template so the message
            # still reaches NEW numbers.
            if s.TWILIO_WHATSAPP_CONTENT_SID and _error_matches(exc, _CONTENTSID_ERRORS):
                logger.info(
                    "[WhatsApp] Twilio requires a template for this number (no 24h "
                    "session) - falling back to ContentSid %s", s.TWILIO_WHATSAPP_CONTENT_SID,
                )
                return self.send_template(contact, template_name="", template_params=None)
            raise
        delivery = self._result(self.name, result)
        logger.info("[WhatsApp] Twilio message ID: %s | Provider accepted message", delivery.provider_message_id)
        return delivery

    def _content_sid(self, s, template_name: str) -> str:
        mapping = s.twilio_whatsapp_templates
        if template_name:
            sid = mapping.get(template_name)
            if sid:
                return sid
            if not s.TWILIO_WHATSAPP_CONTENT_SID:
                raise ProviderConfigError(
                    f"No Twilio ContentSid found for template '{template_name}'. "
                    "Set TWILIO_WHATSAPP_CONTENT_SID (default) or add it to "
                    "TWILIO_WHATSAPP_TEMPLATES in .env."
                )
        if not s.TWILIO_WHATSAPP_CONTENT_SID:
            raise ProviderConfigError(
                "Twilio WhatsApp template provider is not configured. Set "
                "TWILIO_WHATSAPP_CONTENT_SID in .env (the ContentSid of an "
                "approved WhatsApp content template, e.g. HX...)."
            )
        return s.TWILIO_WHATSAPP_CONTENT_SID

    def send_template(
        self,
        contact: str,
        template_name: str,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """
        Send an approved Twilio WhatsApp content template to a contact.

        This is the ONLY way to reach a number that has never messaged the
        business (no 24h session required). `template_name` is resolved to a
        ContentSid via TWILIO_WHATSAPP_TEMPLATES, falling back to
        TWILIO_WHATSAPP_CONTENT_SID.
        """
        s = get_settings()
        if s.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        self._check_config(s)
        sender = self._whatsapp_from(s)
        content_sid = self._content_sid(s, template_name)
        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)

        logger.info("[WhatsApp] Provider: Twilio | From: %s | To: %s", sender, to)
        logger.info("[WhatsApp] Message type: TEMPLATE")
        logger.info("[WhatsApp] ContentSid: %s", content_sid)

        data = {
            "To": f"whatsapp:{to}",
            "From": f"whatsapp:{sender}",
            "ContentSid": content_sid,
        }
        variables = _content_variables(template_params)
        if variables:
            data["ContentVariables"] = variables

        result = self._post(s, data)
        delivery = self._result(self.name, result)
        logger.info("[WhatsApp] Twilio message ID: %s | Provider accepted message", delivery.provider_message_id)
        return delivery

    def send_with_template(
        self,
        contact: str,
        message: str,
        template_name: str,
        template_language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """Send a WhatsApp content template (the `message` argument is ignored
        - templates render their own body)."""
        return self.send_template(contact, template_name=template_name, template_params=template_params)

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        template = payload.get("template") or {}
        if template.get("id"):
            params = {p["name"]: p["value"] for p in (template.get("params") or [])}
            if not params and isinstance(data, dict):
                params = {str(k): str(v) for k, v in data.items()}
            return self.send_with_template(
                recipient,
                message,
                template_name=template["id"],
                template_language=template.get("language"),
                template_params=params,
            )
        return self.send(recipient, message)
