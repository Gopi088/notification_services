"""
Base interface every channel provider must implement, plus shared
exception types used for consistent error handling across providers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """Raised when a provider fails to send a message (config, network, API error)."""


class ProviderConfigError(ProviderError):
    """Raised when required provider credentials/config are missing."""


@dataclass
class ProviderResult:
    provider_name: str
    provider_message_id: str
    status: str  # "sent" (best-effort ack) or "delivered" (mock/instant channels)


class NotificationProvider(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, contact: str, message: str) -> ProviderResult:
        """
        Send `message` to `contact`. Must raise ProviderError (or a subclass)
        on any failure -- never return a partial/ambiguous result.
        """
        raise NotImplementedError
