from app.providers.base import NotificationProvider
from app.providers.email_provider import EmailProvider
from app.providers.sms_provider import SMSProvider
from app.providers.whatsapp_provider import WhatsAppProvider
from app.schemas import Channel

_PROVIDERS = {
    Channel.whatsapp: WhatsAppProvider,
    Channel.sms: SMSProvider,
    Channel.email: EmailProvider,
}


def get_provider(channel: Channel) -> NotificationProvider:
    provider_cls = _PROVIDERS[channel]
    return provider_cls()
