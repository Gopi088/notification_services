"""
All three channels (WhatsApp, SMS, Email) backed by one service:
Azure Communication Services (ACS).

One connection string authenticates everything:
- SMS   : https://learn.microsoft.com/azure/communication-services/quickstarts/sms/send
- Email : https://learn.microsoft.com/azure/communication-services/quickstarts/email/send-email
- WhatsApp: https://learn.microsoft.com/azure/communication-services/quickstarts/advanced-messaging/whatsapp

Requires AZURE_COMMUNICATION_CONNECTION_STRING plus channel settings in .env.
"""
import base64
import ipaddress
import logging
import re
import socket
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import httpx

from app.config import get_settings
from app.providers.base import (
    NotificationProvider,
    ProviderConfigError,
    ProviderError,
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)
from app.templates import TemplateError, render_email

logger = logging.getLogger("azure_provider")

_DIGITS_RE = re.compile(r"[^\d]")
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB per downloaded attachment


def _normalize_phone(contact: str, country_code: str) -> str:
    """Turn '9887270348' / '0-98872-70348' / '+91 98872 70348' into E.164 '+919887270348'."""
    digits = _DIGITS_RE.sub("", contact)
    if len(digits) == 10:
        return f"+{country_code}{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+{country_code}{digits[1:]}"
    if len(digits) == 12 and digits.startswith(country_code):
        return f"+{digits}"
    return f"+{digits}"


class _AzureMixin:
    def __init__(self):
        self.settings = get_settings()

    def _connection_string(self) -> str:
        s = self.settings
        if not s.connection_string or "your_" in s.connection_string:
            raise ProviderConfigError(
                "Azure is not configured. Set COMMUNICATION_SERVICES_CONNECTION_STRING "
                "in .env (Azure portal -> your Communication Services resource -> "
                "Keys -> Connection string)."
            )
        return s.connection_string


class AzureSMSProvider(_AzureMixin, NotificationProvider):
    name = "azure_sms"

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        from azure.communication.sms import SmsClient

        s = self.settings
        if not s.AZURE_SMS_FROM:
            raise ProviderConfigError(
                "SMS provider is not configured. Set AZURE_SMS_FROM in .env "
                "(your SMS-enabled ACS phone number, E.164, e.g. +919812345678)."
            )

        try:
            sms_client = SmsClient.from_connection_string(self._connection_string())
            results = sms_client.send(
                from_=s.AZURE_SMS_FROM,
                to=_normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE),
                message=message,
                enable_delivery_report=True,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "retry")):
                raise ProviderTransientError(f"Azure SMS error: {exc}") from exc
            raise ProviderError(f"Azure SMS error: {exc}") from exc

        result = results[0]
        if not result.successful:
            raise ProviderPermanentError(f"Azure SMS failed for {result.to}: {result.error_message}")

        return ProviderResult(self.name, result.message_id, "sent")

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        return self.send(recipient, message)


class AzureEmailProvider(_AzureMixin, NotificationProvider):
    name = "azure_email"

    def send(self, contact: str, message: str, subject: str = "Notification") -> ProviderResult:
        return self._send_email(contact, message, subject=subject)

    def _send_email(
        self,
        contact: str,
        message: str,
        subject: str = "Notification",
        html: Optional[str] = None,
        cc: Optional[list] = None,
        bcc: Optional[list] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[list] = None,
    ) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        from azure.communication.email import EmailClient

        s = self.settings
        if not s.AZURE_EMAIL_FROM:
            raise ProviderConfigError(
                "Email provider is not configured. Set AZURE_EMAIL_FROM in .env "
                "(a sender verified in the Azure Email Communication Service, "
                "e.g. DoNotReply@yourdomain.com)."
            )

        recipients = {"to": [{"address": contact}]}
        if cc:
            recipients["cc"] = [{"address": addr} for addr in cc]
        if bcc:
            recipients["bcc"] = [{"address": addr} for addr in bcc]

        email_message = {
            "senderAddress": s.AZURE_EMAIL_FROM,
            "recipients": recipients,
            "content": {
                "subject": subject,
                "plainText": message,
                "html": html or render_email(body=message, subject=subject),
            },
        }
        if reply_to:
            email_message["replyTo"] = [{"address": reply_to}]
        if attachments:
            email_message["attachments"] = self._build_attachments(attachments)

        try:
            email_client = EmailClient.from_connection_string(self._connection_string())
            poller = email_client.begin_send(email_message)
            result = poller.result()
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "retry")):
                raise ProviderTransientError(f"Azure Email error: {exc}") from exc
            raise ProviderError(f"Azure Email error: {exc}") from exc

        message_id = result.get("message_id", "") if isinstance(result, dict) else str(result)
        return ProviderResult(self.name, message_id, "sent")

    @staticmethod
    def _build_attachments(attachments: list) -> list:
        built = []
        for att in attachments:
            name = att.get("name") or "attachment"
            content_type = att.get("type") or att.get("contentType") or "application/octet-stream"
            if att.get("content_base64"):
                encoded = att["content_base64"]
            elif att.get("url"):
                encoded = AzureEmailProvider._fetch_as_base64(att["url"])
            else:
                raise ProviderError(f"Attachment '{name}' needs a 'url' or 'content_base64'.")
            built.append({"name": name, "contentType": content_type, "contentInBase64": encoded})
        return built

    @staticmethod
    def _validate_url(url: str) -> None:
        """Reject non-HTTPS URLs, embedded credentials, redirects to other
        hosts, and any resolution to private/loopback/link-local/reserved IPs
        (mitigates SSRF via user-supplied attachment URLs)."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ProviderPermanentError(
                f"Attachment url must use https, got '{parsed.scheme or 'none'}'."
            )
        host = parsed.hostname
        if not host:
            raise ProviderPermanentError(f"Attachment url has no host: {url}")
        if parsed.username or parsed.password:
            raise ProviderPermanentError("Attachment url must not contain credentials.")
        if host.lower() == "localhost":
            raise ProviderPermanentError("Attachment url must not point at localhost.")
        try:
            addr = ipaddress.ip_address(host)
            ips = [addr]
        except ValueError:
            try:
                ips = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, parsed.port or 443)]
            except (socket.gaierror, ValueError) as exc:
                raise ProviderError(f"Could not resolve attachment host '{host}'.") from exc
        for ip in ips:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ProviderPermanentError(f"Attachment url resolves to a blocked address: {ip}.")

    @staticmethod
    def _fetch_as_base64(url: str) -> str:
        AzureEmailProvider._validate_url(url)
        reason: Optional[str] = None
        chunks: list = []
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=False) as resp:
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    reason = f"Attachment url redirects are not allowed: {url}"
                else:
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_ATTACHMENT_BYTES:
                                reason = f"Attachment from {url} exceeds {MAX_ATTACHMENT_BYTES} bytes."
                        except ValueError:
                            pass
                    if reason is None:
                        total = 0
                        for chunk in resp.iter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > MAX_ATTACHMENT_BYTES:
                                reason = f"Attachment from {url} exceeds {MAX_ATTACHMENT_BYTES} bytes."
                                break
                            chunks.append(chunk)
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("timeout", "connection", "network")):
                raise ProviderTransientError(f"Could not download attachment from {url}: {exc}") from exc
            raise ProviderError(f"Could not download attachment from {url}: {exc}") from exc
        if reason is not None:
            raise ProviderPermanentError(reason)
        return base64.b64encode(b"".join(chunks)).decode("ascii")

    def send_with_template(
        self,
        contact: str,
        message: str,
        template_name: str,
        template_language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """
        Send via a local HTML email template from templates/email/<name>.html.
        `template_params` may override the subject via a 'subject' key.
        """
        params = template_params or {}
        subject = params.get("subject", "Notification")
        try:
            html_body = render_email(body=message, subject=subject, template_name=template_name)
        except TemplateError as exc:
            raise ProviderError(str(exc)) from exc
        return self._send_email(contact, message, subject=subject, html=html_body)

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        recipient = payload.get("recipient", "")
        message = payload.get("message") or (str(data) if isinstance(data, str) else "") or ""
        subject = payload.get("subject") or "Notification"
        html = payload.get("html")
        cc = payload.get("cc") or []
        bcc = payload.get("bcc") or []
        reply_to = payload.get("replyTo") or payload.get("reply_to")
        attachments = payload.get("attachments") or []
        return self._send_email(
            recipient,
            message,
            subject=subject,
            html=html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
        )


class AzureWhatsAppProvider(_AzureMixin, NotificationProvider):
    name = "azure_whatsapp"

    def _log(self, *parts: Any) -> None:
        logger.info("[WhatsApp] %s", " ".join(str(p) for p in parts))

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        s = self.settings
        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)
        self._log("Provider: Azure Communication Services")
        self._log("From:", s.whatsapp_from or "channel-linked business number")
        self._log("To:", to)
        self._log("Channel ID:", s.whatsapp_channel_id)
        self._log("Mode:", "REAL" if not s.MOCK_MODE else "MOCK")
        self._log("Message type: TEXT (24h session window)")

        if not s.whatsapp_channel_id:
            raise ProviderConfigError(
                "WhatsApp provider is not configured. Set WHATSAPP_CHANNEL_ID "
                "in .env (WhatsApp channel registration ID from the Azure portal)."
            )

        # Free-form text is ONLY delivered inside a 24h session window (the
        # recipient messaged the business number first). It is sent as-is; it
        # is never swapped for a template, so the caller's message reaches the
        # recipient when a session is open.
        from azure.communication.messages import NotificationMessagesClient
        from azure.communication.messages.models import TextNotificationContent

        self._log("Sending text message...")
        try:
            client = NotificationMessagesClient.from_connection_string(self._connection_string())
            content = TextNotificationContent(
                channel_registration_id=s.whatsapp_channel_id,
                to=[to],
                content=message,
            )
            response = client.send(content)
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "retry")):
                raise ProviderTransientError(f"Azure WhatsApp error: {exc}") from exc
            raise ProviderError(f"Azure WhatsApp error: {exc}") from exc

        if not response.receipts:
            raise ProviderError("Azure WhatsApp returned no delivery receipt.")
        receipt = response.receipts[0]
        if getattr(receipt, "error", None):
            logger.error("[WhatsApp] Azure returned an error for %s: %s", receipt.to, receipt.error)
            raise ProviderPermanentError(f"Azure WhatsApp failed for {receipt.to}: {receipt.error}")

        self._log("Azure message ID:", receipt.message_id)
        self._log("Provider accepted message")
        return ProviderResult(self.name, receipt.message_id, "sent")

    def send_template(
        self,
        contact: str,
        template_name: str,
        language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """
        Send an approved Meta/WhatsApp template to a contact via Azure
        Communication Services Advanced Messaging. This is the ONLY way to
        reach a number that has never messaged the business (no 24h session
        required).

        `template_params` maps template variable names to values, e.g.
        {"name": "Rahul"}. A template with no variables (static body) is sent
        as-is when no params are given.

        The returned status is "sent" (Azure accepted the message). Actual
        delivery (delivered/failed/read) arrives via the Azure delivery-status
        webhook and must not be claimed here.
        """
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        s = self.settings
        to = _normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)

        if not s.connection_string or "your_" in s.connection_string:
            raise ProviderConfigError(
                "Azure is not configured. Set COMMUNICATION_SERVICES_CONNECTION_STRING "
                "in .env (Azure portal -> Communication Services -> Keys -> Connection string)."
            )
        if not s.whatsapp_channel_id:
            raise ProviderConfigError(
                "WhatsApp provider is not configured. Set WHATSAPP_CHANNEL_ID "
                "in .env (WhatsApp channel registration ID from the Azure portal)."
            )
        if not template_name:
            raise ProviderConfigError(
                "No WhatsApp template name given. Provide template_name or set "
                "WHATSAPP_TEMPLATE_NAME in .env."
            )

        lang = language or s.whatsapp_template_language
        params = template_params or {}

        self._log("Provider: Azure Communication Services")
        self._log("From:", s.whatsapp_from or "channel-linked business number")
        self._log("To:", to)
        self._log("Channel ID:", s.whatsapp_channel_id)
        self._log("Mode:", "REAL" if not s.MOCK_MODE else "MOCK")
        self._log("Message type: TEMPLATE")
        self._log("Template:", template_name)
        self._log("Language:", lang)

        from azure.communication.messages import NotificationMessagesClient
        from azure.communication.messages.models import (
            MessageTemplate,
            MessageTemplateText,
            TemplateNotificationContent,
            WhatsAppMessageTemplateBindings,
            WhatsAppMessageTemplateBindingsComponent,
        )

        # Build one binding + value per supplied parameter so templates with
        # variables ({{1}}, {{2}}, ...) are filled in order. Without params,
        # send the template as-is (static templates have no variables).
        if params:
            body_bindings = [
                WhatsAppMessageTemplateBindingsComponent(ref_value=name) for name in params
            ]
            template_values = [
                MessageTemplateText(name=name, text=str(value)) for name, value in params.items()
            ]
            bindings = WhatsAppMessageTemplateBindings(body=body_bindings)
        else:
            bindings = WhatsAppMessageTemplateBindings(body=[])
            template_values = []

        template = MessageTemplate(
            name=template_name,
            language=lang,
            bindings=bindings,
            template_values=template_values,
        )
        content = TemplateNotificationContent(
            channel_registration_id=s.whatsapp_channel_id,
            to=[to],
            template=template,
        )

        self._log("Sending template message...")
        try:
            client = NotificationMessagesClient.from_connection_string(self._connection_string())
            response = client.send(content)
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("timeout", "connection", "network", "retry")):
                raise ProviderTransientError(f"Azure WhatsApp template error: {exc}") from exc
            raise ProviderError(f"Azure WhatsApp template error: {exc}") from exc

        if not response.receipts:
            raise ProviderError("Azure WhatsApp template returned no delivery receipt.")
        receipt = response.receipts[0]
        if getattr(receipt, "error", None):
            logger.error("[WhatsApp] Azure returned an error for %s: %s", receipt.to, receipt.error)
            raise ProviderPermanentError(f"Azure WhatsApp template failed for {receipt.to}: {receipt.error}")

        self._log("Azure message ID:", receipt.message_id)
        self._log("Provider accepted message")
        return ProviderResult(self.name, receipt.message_id, "sent")

    def send_with_template(
        self,
        contact: str,
        message: str,
        template_name: str,
        template_language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """
        Backward-compatible alias of `send_template`. The `message` argument is
        ignored for WhatsApp template sends - templates render their own body.
        """
        return self.send_template(
            contact,
            template_name=template_name,
            language=template_language,
            template_params=template_params,
        )

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