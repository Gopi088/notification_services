from enum import Enum
from typing import Optional


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        http_status: int = 400,
        field: Optional[str] = None,
    ):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "success": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "field": self.field,
            },
        }


class NotFoundError(AppError):
    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(ErrorCode.NOT_FOUND, message, http_status=404, field=field)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Authentication required", field: Optional[str] = None):
        super().__init__(ErrorCode.UNAUTHORIZED, message, http_status=401, field=field)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Insufficient permissions", field: Optional[str] = None):
        super().__init__(ErrorCode.FORBIDDEN, message, http_status=403, field=field)


class RateLimitedError(AppError):
    def __init__(self, message: str = "Rate limit exceeded", field: Optional[str] = None):
        super().__init__(ErrorCode.RATE_LIMITED, message, http_status=429, field=field)


class ValidationError(AppError):
    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(ErrorCode.VALIDATION_ERROR, message, http_status=422, field=field)


class InternalError(AppError):
    def __init__(self, message: str = "Internal server error", field: Optional[str] = None):
        super().__init__(ErrorCode.INTERNAL_ERROR, message, http_status=500, field=field)
