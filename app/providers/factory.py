from app.config import get_settings
from app.providers.azure_provider import (
    AzureEmailProvider,
    AzureSMSProvider,
    AzureWhatsAppProvider,
)
from app.providers.base import NotificationProvider
from app.providers.twilio_provider import TwilioSMSProvider, TwilioWhatsAppProvider
from app.providers.vonage_provider import VonageSMSProvider, VonageWhatsAppProvider
from app.schemas import Channel

_PROVIDERS = {
    Channel.email: AzureEmailProvider,
}


def get_provider(channel: Channel) -> NotificationProvider:
    settings = get_settings()
    twilio_ready = bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)
    if channel == Channel.sms:
        selected = settings.SMS_PROVIDER.strip().lower()
        if selected == "twilio":
            return TwilioSMSProvider()
        if selected == "vonage":
            return VonageSMSProvider()
        if selected == "azure":
            return AzureSMSProvider()
        # ``auto`` is intentionally limited to compatibility/local use.
        if selected != "auto":
            raise ValueError("SMS_PROVIDER must be one of: azure, vonage, twilio, auto")
        if not settings.MOCK_MODE:
            raise ValueError("SMS_PROVIDER=auto is not allowed when MOCK_MODE=false; select a provider explicitly")
        if twilio_ready and settings.TWILIO_FROM:
            return TwilioSMSProvider()
        if settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageSMSProvider()
        return AzureSMSProvider()
    if channel == Channel.whatsapp:
        selected = settings.WHATSAPP_PROVIDER.strip().lower()
        if selected == "twilio":
            return TwilioWhatsAppProvider()
        if selected == "vonage":
            return VonageWhatsAppProvider()
        if selected == "azure":
            return AzureWhatsAppProvider()
        if selected != "auto":
            raise ValueError("WHATSAPP_PROVIDER must be one of: azure, vonage, twilio, auto")
        if not settings.MOCK_MODE:
            raise ValueError("WHATSAPP_PROVIDER=auto is not allowed when MOCK_MODE=false; select a provider explicitly")
        if twilio_ready and settings.twilio_whatsapp_from:
            return TwilioWhatsAppProvider()
        if settings.VONAGE_WHATSAPP_FROM and settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageWhatsAppProvider()
        return AzureWhatsAppProvider()
    return _PROVIDERS[channel]()
