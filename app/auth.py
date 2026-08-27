"""
OAuth-style authentication with Bearer tokens + API key fallback.

Two ways to authenticate:

1. OAuth Bearer Token (primary):
   - POST /api/v1/auth/token with client_id + client_secret
   - Receive JWT access_token
   - Send header: Authorization: Bearer <token>

2. API Key (legacy fallback):
   - Send header: X-API-Key: <key>

Both methods validate credentials against the api_keys table.
Scopes are embedded in JWT tokens for authorization checks.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

import jwt
from fastapi import Header, Request

from app.config import get_settings
from app.database import get_api_key, hash_api_key
from app.errors import AppError, ErrorCode, ForbiddenError, UnauthorizedError

logger = logging.getLogger("auth")

_API_KEY_CTX_ATTR = "api_key_ctx"


@dataclass(frozen=True)
class APIKeyContext:
    """Immutable context attached to every authenticated request."""
    key_hash: str
    name: str
    tenant_id: str
    scopes: List[str]
    rate_limit_per_second: Optional[int]


def require_api_key(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> None:
    """
    FastAPI dependency — validates either OAuth Bearer token or API key.

    Priority:
    1. Authorization: Bearer <jwt_token>  (OAuth flow)
    2. X-API-Key: <raw_key>               (legacy flow)
    """
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        return

    # --- Try OAuth Bearer token first ---
    if authorization.startswith("Bearer "):
        token = authorization[7:]
        _validate_bearer_token(request, token)
        return

    # --- Fallback to API key ---
    if x_api_key:
        _validate_api_key(request, x_api_key)
        return

    # --- No credentials ---
    logger.warning("Auth failed: no credentials provided")
    _audit_auth_failure("Missing credentials (no Bearer token or X-API-Key)")
    raise UnauthorizedError("Authentication required. Provide Authorization: Bearer <token> or X-API-Key header.")


def _validate_bearer_token(request: Request, token: str) -> None:
    """Validate a JWT Bearer token and set request context."""
    settings = get_settings()

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.warning("Auth failed: token expired")
        _audit_auth_failure("OAuth: expired token")
        raise UnauthorizedError("Token has expired. Request a new one via /auth/token.")
    except jwt.InvalidTokenError:
        logger.warning("Auth failed: invalid token")
        _audit_auth_failure("OAuth: invalid token")
        raise UnauthorizedError("Invalid token.")

    # Look up the key record to check if still active
    client_name = payload.get("sub", "")
    key_hash = hash_api_key(settings.AUTH_API_KEY)  # We verify against stored keys
    key_record = None

    # Find key by name
    from app.database import list_api_keys
    all_keys = list_api_keys()
    for k in all_keys:
        if k.get("name") == client_name:
            key_record = k
            break

    if key_record and not key_record.get("is_active", True):
        logger.warning("Auth failed: key revoked (client=%s)", client_name)
        _audit_auth_failure("OAuth: revoked key")
        raise UnauthorizedError("Client credentials have been revoked.")

    ctx = APIKeyContext(
        key_hash=key_record.get("key_hash", "") if key_record else "",
        name=client_name,
        tenant_id=payload.get("tenant_id", ""),
        scopes=payload.get("scopes", []),
        rate_limit_per_second=key_record.get("rate_limit_per_second") if key_record else None,
    )

    logger.info(
        "OAuth auth passed: client=%s tenant=%s scopes=%s",
        ctx.name, ctx.tenant_id, ctx.scopes,
    )
    request.state.api_key_ctx = ctx


def _validate_api_key(request: Request, x_api_key: str) -> None:
    """Validate an API key (legacy flow) and set request context."""
    key_hash = hash_api_key(x_api_key)
    key_record = get_api_key(key_hash)

    if key_record is None:
        logger.warning("Auth failed: unknown API key (hash=%s)", key_hash[:12])
        _audit_auth_failure("Unknown API key")
        raise UnauthorizedError("Invalid or unknown API key.")

    if not key_record.get("is_active", False):
        logger.warning("Auth failed: key revoked (hash=%s)", key_hash[:12])
        _audit_auth_failure("Revoked API key")
        raise UnauthorizedError("API key has been revoked.")

    expires_at = key_record.get("expires_at")
    if expires_at:
        from datetime import datetime, timezone
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                logger.warning("Auth failed: key expired (hash=%s)", key_hash[:12])
                _audit_auth_failure("Expired API key")
                raise UnauthorizedError("API key has expired.")
        except ValueError:
            pass

    ctx = APIKeyContext(
        key_hash=key_hash,
        name=key_record.get("name", ""),
        tenant_id=key_record.get("tenant_id", ""),
        scopes=key_record.get("scopes", []),
        rate_limit_per_second=key_record.get("rate_limit_per_second"),
    )

    logger.info(
        "API key auth passed: key=%s tenant=%s scopes=%s",
        ctx.name, ctx.tenant_id, ctx.scopes,
    )
    request.state.api_key_ctx = ctx


def _get_ctx(request: Request) -> Optional[APIKeyContext]:
    return getattr(request.state, "api_key_ctx", None)


def _audit_auth_failure(reason: str) -> None:
    from app.audit import record as audit_record
    audit_record(
        action="auth.failure",
        outcome="denied",
        detail={"reason": reason},
    )


def require_scope(*required_scopes: str):
    """
    Returns a FastAPI dependency that asserts the authenticated client
    holds at least one of the given scopes.

    Usage::

        @router.post(
            "/send",
            dependencies=[Depends(require_api_key), Depends(require_scope("send:write"))],
        )
    """
    def _check(request: Request):
        ctx = _get_ctx(request)
        if ctx is None:
            # AUTH_ENABLED=false — no key context, no scopes to check
            return
        if not any(s in ctx.scopes for s in required_scopes):
            logger.warning(
                "Authz failed: client=%s tenant=%s required=%s have=%s",
                ctx.name, ctx.tenant_id, required_scopes, ctx.scopes,
            )
            from app.audit import record as audit_record
            audit_record(
                action="authz.failure",
                outcome="denied",
                detail={
                    "reason": "Missing required scope",
                    "required": list(required_scopes),
                    "available": ctx.scopes,
                },
            )
            raise ForbiddenError(
                f"Missing required scope. Required one of: {', '.join(required_scopes)}"
            )
    return _check


def get_api_key_context(request: Request) -> Optional[APIKeyContext]:
    """Convenience accessor for route handlers that need the context."""
    return _get_ctx(request)
