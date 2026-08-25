"""
Base interface every channel provider must implement, plus shared
exception types used for consistent error handling across providers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


class ProviderError(Exception):
    """Raised when a provider fails to send a message (config, network, API error)."""


class ProviderConfigError(ProviderError):
    """Raised when required provider credentials/config are missing."""


class ProviderTransientError(ProviderError):
    """Network timeouts, HTTP 429/5xx -- safe to retry."""


class ProviderPermanentError(ProviderError):
    """Bad credentials (401/403), invalid recipient, rejected payload (400) -- never retry."""


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

    def send_with_template(
        self,
        contact: str,
        message: str,
        template_name: str,
        template_language: Optional[str] = None,
        template_params: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        """
        Send via an external template instead of free-form text.
        Default implementation falls back to plain send; providers that support
        templates (e.g. WhatsApp) override this.
        """
        return self.send(contact, message)

    def send_delivery(self, payload: Dict[str, Any], data: Any = None) -> ProviderResult:
        """
        Send a channel-specific delivery payload (a dict) plus optional event
        data. Each provider knows its own payload shape; rich providers
        (email html/cc/attachments, WhatsApp templates, ...) override this.
        """
        return self.send(
            payload.get("recipient", ""),
            payload.get("message", ""),
        )
