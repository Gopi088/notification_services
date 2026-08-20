from app.providers.azure_provider import (
    AzureEmailProvider,
    AzureSMSProvider,
    AzureWhatsAppProvider,
)
from app.providers.base import NotificationProvider
from app.schemas import Channel

_PROVIDERS = {
    Channel.whatsapp: AzureWhatsAppProvider,
    Channel.sms: AzureSMSProvider,
    Channel.email: AzureEmailProvider,
}


def get_provider(channel: Channel) -> NotificationProvider:
    provider_cls = _PROVIDERS[channel]
    return provider_cls()