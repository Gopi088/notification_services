"""
All three channels (WhatsApp, SMS, Email) backed by one service:
Azure Communication Services (ACS).

One connection string authenticates everything:
- SMS   : https://learn.microsoft.com/azure/communication-services/quickstarts/sms/send
- Email : https://learn.microsoft.com/azure/communication-services/quickstarts/email/send-email
- WhatsApp: https://learn.microsoft.com/azure/communication-services/quickstarts/advanced-messaging/whatsapp

Requires AZURE_COMMUNICATION_CONNECTION_STRING plus channel settings in .env.
"""
import re
import uuid

from app.config import get_settings
from app.providers.base import NotificationProvider, ProviderConfigError, ProviderError, ProviderResult

_DIGITS_RE = re.compile(r"[^\d]")


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
        if not s.AZURE_COMMUNICATION_CONNECTION_STRING or "your_" in s.AZURE_COMMUNICATION_CONNECTION_STRING:
            raise ProviderConfigError(
                "Azure is not configured. Set AZURE_COMMUNICATION_CONNECTION_STRING "
                "in .env (Azure portal -> your Communication Services resource -> "
                "Keys -> Connection string)."
            )
        return s.AZURE_COMMUNICATION_CONNECTION_STRING


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
        except Exception as exc:  # noqa: BLE001 - surface SDK errors cleanly
            raise ProviderError(f"Azure SMS error: {exc}") from exc

        result = results[0]
        if not result.successful:
            raise ProviderError(f"Azure SMS failed for {result.to}: {result.error_message}")

        return ProviderResult(self.name, result.message_id, "sent")


class AzureEmailProvider(_AzureMixin, NotificationProvider):
    name = "azure_email"

    def send(self, contact: str, message: str) -> ProviderResult:
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

        email_message = {
            "senderAddress": s.AZURE_EMAIL_FROM,
            "recipients": {"to": [{"address": contact}]},
            "content": {"subject": "Notification", "plainText": message},
        }

        try:
            email_client = EmailClient.from_connection_string(self._connection_string())
            poller = email_client.begin_send(email_message)
            result = poller.result()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Azure Email error: {exc}") from exc

        message_id = result.get("message_id", "") if isinstance(result, dict) else str(result)
        return ProviderResult(self.name, message_id, "sent")


class AzureWhatsAppProvider(_AzureMixin, NotificationProvider):
    name = "azure_whatsapp"

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        from azure.communication.messages import NotificationMessagesClient
        from azure.communication.messages.models import TextNotificationContent

        s = self.settings
        if not s.AZURE_WHATSAPP_CHANNEL_ID:
            raise ProviderConfigError(
                "WhatsApp provider is not configured. Set AZURE_WHATSAPP_CHANNEL_ID "
                "in .env (WhatsApp channel registration ID from the Azure portal)."
            )

        try:
            client = NotificationMessagesClient.from_connection_string(self._connection_string())
            content = TextNotificationContent(
                channel_registration_id=s.AZURE_WHATSAPP_CHANNEL_ID,
                to=[_normalize_phone(contact, s.AZURE_DEFAULT_COUNTRY_CODE)],
                content=message,
            )
            response = client.send(content)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Azure WhatsApp error: {exc}") from exc

        if not response.receipts:
            raise ProviderError("Azure WhatsApp returned no delivery receipt.")
        receipt = response.receipts[0]
        if getattr(receipt, "error", None):
            raise ProviderError(f"Azure WhatsApp failed for {receipt.to}: {receipt.error}")

        return ProviderResult(self.name, receipt.message_id, "sent")