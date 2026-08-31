"""JWT authentication for notification API routes.

Client auth flow:
    1. POST /api/v1/auth/login  {client_id, client_secret}  -> {access_token}
    2. Use the token on /api/v1/*  via  Authorization: Bearer <JWT>

When AUTH_ENABLED is false (dev/tests) auth is bypassed and requests are
treated as `anonymous`. When enabled, every /api/v1/* route requires a valid
Bearer JWT signed with JWT_SECRET_KEY.

Secrets (JWTs, passwords, Authorization headers) are never logged.
Webhook/provider authentication (Twilio signatures, Azure Event Grid
validation) is completely separate from client JWT auth.
"""
import datetime
import hashlib
import hmac
import logging
import secrets

import jwt
from fastapi import Header, HTTPException, Request

from app.config import get_settings

logger = logging.getLogger("auth")

_AUTH_ERR = {
    "success": False,
    "error": {"code": "unauthorized", "message": "Not authenticated.", "field": None},
}


def create_access_token(subject: str, user_id: str, expires_minutes: int | None = None) -> str:
    """Issue a signed JWT access token carrying sub + user_id claims."""
    s = get_settings()
    now = datetime.datetime.now(datetime.timezone.utc)
    expire = now + datetime.timedelta(
        minutes=expires_minutes if expires_minutes is not None
        else s.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "user_id": user_id,
        "iat": now,
        "exp": expire,
        "iss": s.APP_NAME,
    }
    return jwt.encode(payload, s.JWT_SECRET_KEY, algorithm=s.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Validate signature, expiration, algorithm and required claims.

    Returns the verified claims. Raises HTTPException 401 for any failure
    (missing/expired/invalid token, wrong secret, wrong algorithm).
    """
    s = get_settings()
    if not s.JWT_SECRET_KEY:
        logger.debug("auth rejected reason=server_configuration")
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": {
                "code": "server_config_error",
                "message": "JWT_SECRET_KEY is not set in .env.",
                "field": None,
            }},
        )
    try:
        claims = jwt.decode(
            token,
            s.JWT_SECRET_KEY,
            algorithms=[s.JWT_ALGORITHM],
            options={"require": ["sub", "user_id", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        logger.debug("auth rejected reason=token_expired")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"}, detail=_AUTH_ERR)
    except (jwt.InvalidTokenError, jwt.DecodeError, jwt.InvalidAlgorithmError):
        logger.debug("auth rejected reason=invalid_token")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"}, detail=_AUTH_ERR)
    if not claims.get("sub") or not claims.get("user_id"):
        logger.debug("auth rejected reason=missing_claims")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"}, detail=_AUTH_ERR)
    return claims


def extract_bearer_token(authorization: str) -> str:
    """Return the Bearer token from an Authorization header value, or ''."""
    if not authorization:
        return ""
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def user_id_from_request(request) -> str:
    """Return the authenticated user_id from the JWT, or 'anonymous'.

    Falls back to the legacy hashed X-API-Key identity for callers that still
    send it, preserving behavior when AUTH_ENABLED=false.
    """
    token = extract_bearer_token(request.headers.get("Authorization", ""))
    if token and get_settings().JWT_SECRET_KEY:
        try:
            claims = decode_token(token)
            return str(claims.get("user_id") or claims.get("sub") or "anonymous")
        except HTTPException:
            pass
    key = request.headers.get("X-API-Key", "")
    if not key:
        return "anonymous"
    return f"usr_{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def require_auth(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> str:
    """Dependency: enforce JWT auth on /api/v1/* routes when AUTH_ENABLED.

    Returns the authenticated user_id. Raises 401 for missing/invalid/expired
    tokens (when auth is enabled).
    """
    from app.audit import record_audit

    settings = get_settings()
    if not settings.AUTH_ENABLED:
        logger.debug("auth bypassed enabled=false")
        return "anonymous"

    # When invoked directly (unit tests) the Header defaults arrive as
    # fastapi.Header objects rather than strings - normalize them.
    if not isinstance(authorization, str):
        authorization = ""
    if not isinstance(x_api_key, str):
        x_api_key = ""

    if not settings.JWT_SECRET_KEY:
        logger.debug("auth rejected reason=server_configuration")
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": {
                "code": "server_config_error",
                "message": "AUTH_ENABLED=true but JWT_SECRET_KEY is not set in .env.",
                "field": None,
            }},
        )

    token = extract_bearer_token(authorization)
    if not token:
        # Backward-compatible fallback: accept the legacy X-API-Key when set.
        if settings.AUTH_API_KEY and x_api_key and secrets.compare_digest(x_api_key, settings.AUTH_API_KEY):
            return f"usr_{hashlib.sha256(x_api_key.encode()).hexdigest()[:16]}"
        record_audit(user_id="anonymous", action="authentication_failed",
                     result="failure", failure_reason="missing_token")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"}, detail=_AUTH_ERR)

    try:
        claims = decode_token(token)
    except HTTPException as exc:
        record_audit(user_id="anonymous", action="authentication_failed",
                     result="failure", failure_reason="invalid_token")
        raise exc

    user_id = str(claims.get("user_id") or claims.get("sub") or "anonymous")
    logger.debug("auth accepted user_id=%s", user_id)
    return user_id


def validate_client_credentials(client_id: str, client_secret: str) -> bool:
    """Constant-time check of login credentials (client_id + client_secret)."""
    s = get_settings()
    expected_secret = s.auth_client_secret_effective
    if not expected_secret:
        return False
    id_ok = hmac.compare_digest(client_id, s.AUTH_CLIENT_ID)
    secret_ok = secrets.compare_digest(client_secret, expected_secret)
    return id_ok and secret_ok
