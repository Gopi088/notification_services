from app.config import get_settings
from app.providers.azure_provider import (
    AzureEmailProvider,
    AzureSMSProvider,
    AzureWhatsAppProvider,
)
from app.providers.base import NotificationProvider
from app.providers.vonage_provider import VonageSMSProvider, VonageWhatsAppProvider
from app.schemas import Channel

_PROVIDERS = {
    Channel.email: AzureEmailProvider,
}


def get_provider(channel: Channel) -> NotificationProvider:
    settings = get_settings()
    if channel == Channel.sms:
        # Prefer Vonage when its credentials are configured, else Azure.
        if settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageSMSProvider()
        return AzureSMSProvider()
    if channel == Channel.whatsapp:
        # Prefer the Vonage WhatsApp Sandbox when its sender is configured,
        # else Azure.
        if settings.VONAGE_WHATSAPP_FROM and settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET:
            return VonageWhatsAppProvider()
        return AzureWhatsAppProvider()
    return _PROVIDERS[channel]()