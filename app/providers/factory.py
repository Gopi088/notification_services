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
        # Prefer Twilio when configured, then Vonage, else Azure.
        if twilio_ready and settings.TWILIO_FROM:
            return TwilioSMSProvider()
        if settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageSMSProvider()
        return AzureSMSProvider()
    if channel == Channel.whatsapp:
        # Prefer Twilio when configured, then Vonage, else Azure.
        if twilio_ready and settings.twilio_whatsapp_from:
            return TwilioWhatsAppProvider()
        if settings.VONAGE_WHATSAPP_FROM and settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageWhatsAppProvider()
        return AzureWhatsAppProvider()
    return _PROVIDERS[channel]()