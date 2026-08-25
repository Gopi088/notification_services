"""
Centralized, structured error handling.

Every layer raises a typed exception; the API layer maps each to an HTTP status
and a uniform error envelope:

    {"success": false, "error": {"code": "...", "message": "...", "field": ...}}

Codes:
- validation_error       400
- unauthorized           401
- forbidden              403
- not_found              404
- idempotency_conflict   409
- unprocessable_entity   422
- rate_limited           429
- provider_unavailable   502
- queue_unavailable      503
- db_unavailable         503
- internal_error         500
"""
from typing import Any, Dict, Optional


class AppError(Exception):
    """Base application error with an HTTP status and machine-readable code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "Internal server error.",
                 field: Optional[str] = None, **context: Any):
        super().__init__(message)
        self.message = message
        self.field = field
        self.context = context

    def to_dict(self) -> Dict:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            error["field"] = self.field
        return error


class ValidationError(AppError):
    status_code = 400
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class IdempotencyConflictError(AppError):
    status_code = 409
    code = "idempotency_conflict"


class UnprocessableError(AppError):
    status_code = 422
    code = "unprocessable_entity"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class ProviderUnavailableError(AppError):
    status_code = 502
    code = "provider_unavailable"


class QueueUnavailableError(AppError):
    status_code = 503
    code = "queue_unavailable"


class DatabaseUnavailableError(AppError):
    status_code = 503
    code = "db_unavailable"


class ConfigurationError(AppError):
    status_code = 500
    code = "server_config_error"


def classify_provider_error(error: Exception) -> AppError:
    """Map a provider-layer exception to a typed API error."""
    from app.providers.base import ProviderConfigError, ProviderError

    if isinstance(error, ProviderConfigError):
        return ConfigurationError(str(error))
    if isinstance(error, ProviderError):
        if getattr(error, "retryable", False):
            return ProviderUnavailableError(str(error))
        return ValidationError(str(error))
    return AppError(str(error))
