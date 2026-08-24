"""
API key authentication.

When AUTH_ENABLED=true every /api/v1 request must include the header:

    X-API-Key: <AUTH_API_KEY>

Identity is the API key itself; it is never exposed in responses or logs.
"""
import hmac
import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not get_settings().AUTH_ENABLED:
        return
    expected = get_settings().AUTH_API_KEY
    if not expected:
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": {
                    "code": "server_config_error",
                    "message": "AUTH_ENABLED=true but AUTH_API_KEY is not set in .env.",
                    "field": None,
                },
            },
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": "ApiKey"},
            detail={
                "success": False,
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key. Send X-API-Key header.",
                    "field": None,
                },
            },
        )
