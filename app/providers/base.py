"""
Base interface every channel provider must implement, plus shared
exception types used for consistent error handling across providers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Any, Dict, Optional


class ProviderError(Exception):
    """Raised when a provider fails to send a message (config, network, API error)."""

    def __init__(self, message: str, *, retryable: bool = False, error_code: Optional[str] = None):
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code


class ProviderConfigError(ProviderError):
    """Raised when required provider credentials/config are missing (never retryable)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, error_code="config_error")


_SENSITIVE_ERROR_VALUE = re.compile(
    r"(?i)\b(authorization|api[_ -]?key|token|secret|password|connection[_ -]?string)\b\s*[:=]\s*[^\s,;]+"
)
_URL_CREDENTIALS = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


def sanitize_provider_error(error: Exception | str, *, limit: int = 512) -> str:
    """Return a bounded provider diagnostic without credentials."""
    message = str(error).replace("\n", " ").replace("\r", " ")
    message = _SENSITIVE_ERROR_VALUE.sub(lambda match: f"{match.group(1)}=***", message)
    message = _URL_CREDENTIALS.sub("://***:***@", message)
    return message[:limit]


@dataclass
class ProviderResult:
    provider_name: str
    provider_message_id: str
    status: str  # "submitted" (best-effort ack) or "delivered" (mock/instant channels)


class NotificationProvider(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, contact: str, message: str) -> ProviderResult:
        """
        Send `message` to `contact`. Must raise ProviderError (or a subclass)
        on any failure -- never return a partial/ambiguous result.

        Timeouts, network failures, 429 and 5xx must be raised with
        retryable=True. Validation/credential/4xx failures must be raised with
        retryable=False.
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

    def poll_status(self, provider_message_id: str) -> Optional[str]:
        """
        Query the provider for the current delivery status of a message.

        Returns the raw provider status string (e.g. "delivered", "failed",
        "sent", "read") or None when the provider does not support polling.
        Used for on-demand delivery checks when webhooks are unavailable.
        """
        return None
