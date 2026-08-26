"""
Multi-key API authentication with DB-backed key store.

When AUTH_ENABLED=true every /api/v1 request must include the header:

    X-API-Key: <key>

The key is SHA-256 hashed and looked up in the api_keys table.  Each key
carries metadata used by downstream middleware:

    - tenant_id       (for future multi-tenant isolation)
    - scopes          (for Phase 4 authorization)
    - rate_limit_per_second (for Phase 5 rate limiting)
    - is_active / expires_at (for key lifecycle)

A dataclass `APIKeyContext` is stored on request.state so routers and
middleware can access the authenticated identity without re-querying.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

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


def require_api_key(request: Request, x_api_key: str = Header(default="")) -> None:
    """
    FastAPI dependency (used in ``dependencies=[...]``).

    Stores an ``APIKeyContext`` on ``request.state.api_key_ctx`` when
    auth is enabled and the key is valid.  Returns None in all cases;
    scope checks are handled by ``require_scope``.
    """
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        return

    if not x_api_key:
        logger.warning("Auth failed: missing X-API-Key header")
        _audit_auth_failure("Missing X-API-Key header")
        raise UnauthorizedError("Missing X-API-Key header.")

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
        "Auth passed: key=%s tenant=%s scopes=%s",
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
    Returns a FastAPI dependency that asserts the authenticated API key
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
                "Authz failed: key=%s tenant=%s required=%s have=%s",
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
