"""Optional API-key authentication for notification API routes."""
import hashlib
import logging
import secrets

from fastapi import Header, HTTPException

from app.config import get_settings

logger = logging.getLogger("auth")


def user_id_from_request(request) -> str:
    """Return an anonymous or non-reversible API-key-derived identity."""
    key = request.headers.get("X-API-Key", "")
    if not key:
        return "anonymous"
    return f"usr_{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Enforce the configured API key only when AUTH_ENABLED is true."""
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        logger.debug("auth bypassed enabled=false")
        return
    if not settings.AUTH_API_KEY:
        logger.debug("auth rejected reason=server_configuration")
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": {
                "code": "server_config_error",
                "message": "AUTH_ENABLED=true but AUTH_API_KEY is not set in .env.",
                "field": None,
            }},
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.AUTH_API_KEY):
        logger.debug("auth rejected reason=invalid_or_missing_key")
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": "ApiKey"},
            detail={"success": False, "error": {
                "code": "unauthorized",
                "message": "Invalid or missing API key. Send X-API-Key header.",
                "field": None,
            }},
        )
    logger.debug("auth accepted")
