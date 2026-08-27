"""
Versioned public API (v1).

Single channel per request. One send = one channel = one contact.
Supports Idempotency-Key header for exactly-once semantics.
"""
import logging
import time

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from pydantic import BaseModel

from app import __version__
from app.audit import list_audit, record as audit_record
from app.auth import require_api_key, require_scope
from app.config import get_settings
from app.errors import IdempotencyConflictError, NotFoundError, ValidationError
from app.orchestrator import (
    get_group_summary,
    get_message_summary,
    orchestrate_send,
)
from app.schemas import (
    ChannelStatus,
    ErrorResponse,
    HealthResponse,
    SendRequest,
    SendResponse,
    StatusResponse,
)
from app.validation import ContactValidationError, validate_contact

router = APIRouter(prefix="/api/v1", tags=["v1"])
logger = logging.getLogger("v1")

_settings = get_settings()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and configuration overview",
    dependencies=[Depends(require_api_key)],
)
def health() -> HealthResponse:
    return HealthResponse(
        service=_settings.APP_NAME,
        version=__version__,
        mock_mode=_settings.MOCK_MODE,
    )


@router.post(
    "/notifications/send",
    response_model=SendResponse,
    status_code=202,
    summary="Send a notification on ONE channel (email/sms/whatsapp)",
    description=(
        "Send a single notification to one contact on one channel. "
        "Specify `channel` (email/sms/whatsapp) and `contact` (email address or phone)."
    ),
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key), Depends(require_scope("send:write"))],
)
async def send(
    background_tasks: BackgroundTasks,
    payload: SendRequest,
    idempotency_key: str = Header(default=""),
) -> SendResponse:
    # --- Idempotency check ---
    if idempotency_key:
        from app.config import get_settings as _cfg
        from app.database import create_idempotency, get_idempotency, update_idempotency
        from app.middleware import request_id_var

        existing = get_idempotency(idempotency_key)
        if existing:
            if existing["status"] == "processing":
                logger.warning("Idempotency key in progress: key=%s", idempotency_key[:16])
                raise IdempotencyConflictError(
                    "A request with this Idempotency-Key is already being processed."
                )
            if existing["status"] == "completed" and existing.get("response_body"):
                logger.info("Idempotency hit: key=%s message_id=%s", idempotency_key[:16], existing["message_id"])
                from fastapi import Response as _Resp
                from starlette.responses import JSONResponse as _JSONResp
                return _JSONResp(status_code=200, content=existing["response_body"])
            # If status is "failed", allow a fresh retry — delete old record
            from app.database import get_connection
            with get_connection() as conn:
                conn.execute("DELETE FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,))

        # Reserve this key immediately to prevent concurrent duplicates
        placeholder_id = str(__import__("uuid").uuid4())
        create_idempotency(
            key=idempotency_key,
            message_id=placeholder_id,
            status="processing",
            ttl_hours=_cfg().IDEMPOTENCY_TTL_HOURS,
        )
        logger.info("Idempotency key reserved: key=%s", idempotency_key[:16])

    # Validate contact for the specified channel
    try:
        validate_contact(payload.channel, payload.contact)
    except ContactValidationError as exc:
        logger.warning(
            "Validation failed: channel=%s error=%s",
            payload.channel.value, exc,
            extra={"channel": payload.channel.value},
        )
        # Update idempotency record to failed
        if idempotency_key:
            from app.database import update_idempotency
            update_idempotency(idempotency_key, status="failed")
        raise ValidationError(str(exc), field="contact") from exc

    logger.info(
        "Validation passed: channel=%s",
        payload.channel.value,
        extra={"channel": payload.channel.value},
    )

    summary = orchestrate_send(payload, background_tasks)

    response = SendResponse(
        success=True,
        message_id=summary["message_id"],
        reference=summary.get("reference"),
        channel=summary["channel"],
        contact=summary["contact"],
        status="queued",
    )

    # Finalize idempotency record with real message_id and cached response
    if idempotency_key:
        from app.database import get_connection
        with get_connection() as conn:
            conn.execute(
                """UPDATE idempotency_keys
                   SET message_id = ?, status = 'completed', response_body = ?
                   WHERE idempotency_key = ?""",
                (summary["message_id"], response.model_dump_json(), idempotency_key),
            )
        logger.info("Idempotency record finalized: key=%s message_id=%s", idempotency_key[:16], summary["message_id"])

    return response


@router.get(
    "/notifications/{notification_id}/status",
    response_model=StatusResponse,
    summary="Delivery status for a single message",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key)],
)
def status(notification_id: str) -> StatusResponse:
    # Try as single message
    single = get_message_summary(notification_id)
    if single is not None:
        return StatusResponse(
            success=True,
            message_id=single["message_id"],
            status=single["status"],
            channel=ChannelStatus(
                message_id=single["message_id"],
                channel=single["channel"],
                contact=single["contact"],
                status=single["status"],
                provider=single["provider"],
                provider_message_id=single["provider_message_id"],
                error=single["error"],
                created_at=single["created_at"],
                updated_at=single["updated_at"],
                attempt_count=single.get("attempt_count", 0),
                elapsed_seconds=single.get("elapsed_seconds"),
                timed_out=single.get("timed_out", False),
                delivery_timeout_seconds=single.get("delivery_timeout_seconds"),
            ),
        )

    # Try as legacy group
    group = get_group_summary(notification_id)
    if group is not None:
        ch = group["channels"][0] if group["channels"] else {}
        return StatusResponse(
            success=True,
            message_id=group["message_id"],
            reference=group.get("reference"),
            status=group["status"],
            channel=ChannelStatus(**ch) if ch else ChannelStatus(
                message_id=notification_id,
                channel="unknown",
                contact="unknown",
                status="unknown",
                created_at="",
                updated_at="",
            ),
        )

    raise NotFoundError(f"No notification found with id '{notification_id}'")


@router.get(
    "/admin/audit",
    summary="Audit log (admin only)",
    dependencies=[Depends(require_api_key), Depends(require_scope("admin:read"))],
)
def get_audit_log(
    action: str = Query(default=None, description="Filter by action (substring match)"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    records = list_audit(limit=limit, action_filter=action)
    return {"success": True, "count": len(records), "records": records}


# ---------------------------------------------------------------------------
# Admin: API Key management
# ---------------------------------------------------------------------------

@router.post(
    "/admin/api-keys",
    summary="Create a new API key (admin only)",
    dependencies=[Depends(require_api_key), Depends(require_scope("admin:write"))],
)
def create_key(
    name: str = Query(..., min_length=1, max_length=64),
    tenant_id: str = Query(..., min_length=1, max_length=64),
    scopes: str = Query(default="send:write", description="Comma-separated scopes"),
    rate_limit_per_second: int = Query(default=None, ge=1),
) -> dict:
    import secrets as _secrets
    from app.database import create_api_key, hash_api_key

    raw_key = _secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]

    create_api_key(
        key_hash=key_hash,
        name=name,
        tenant_id=tenant_id,
        scopes=scope_list,
        rate_limit_per_second=rate_limit_per_second,
    )

    audit_record(
        action="admin.api_key.create",
        outcome="success",
        detail={"name": name, "tenant_id": tenant_id, "scopes": scope_list},
    )
    logger.info("Created API key: name=%s tenant=%s scopes=%s", name, tenant_id, scope_list)

    return {
        "success": True,
        "key": raw_key,
        "name": name,
        "tenant_id": tenant_id,
        "scopes": scope_list,
        "message": "Save this key now — it will not be shown again.",
    }


@router.get(
    "/admin/api-keys",
    summary="List API keys (admin only, hashes only)",
    dependencies=[Depends(require_api_key), Depends(require_scope("admin:read"))],
)
def list_keys(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    from app.database import list_api_keys
    keys = list_api_keys(limit=limit)
    return {"success": True, "count": len(keys), "keys": keys}


@router.delete(
    "/admin/api-keys/{key_hash}",
    summary="Revoke an API key (admin only)",
    dependencies=[Depends(require_api_key), Depends(require_scope("admin:write"))],
)
def revoke_key(key_hash: str) -> dict:
    from app.database import revoke_api_key
    revoked = revoke_api_key(key_hash)
    if not revoked:
        raise NotFoundError(f"No active API key with hash '{key_hash[:16]}...'")

    audit_record(
        action="admin.api_key.revoke",
        outcome="success",
        detail={"key_hash": key_hash[:16]},
    )
    logger.info("Revoked API key: hash=%s", key_hash[:16])
    return {"success": True, "message": "API key revoked."}


# ---------------------------------------------------------------------------
# OAuth Token Endpoint
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    client_id: str
    client_secret: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: str


@router.post(
    "/auth/token",
    summary="OAuth token endpoint - exchange client credentials for JWT token",
    response_model=TokenResponse,
)
def get_token(payload: TokenRequest):
    """
    OAuth flow:
    1. Client sends client_id + client_secret
    2. Server validates credentials against api_keys table
    3. Server returns JWT token with expiry and scopes
    4. Client uses token in Authorization: Bearer <token> header
    """
    from app.database import hash_api_key, get_api_key

    key_hash = hash_api_key(payload.client_secret)
    key_record = get_api_key(key_hash)

    if key_record is None:
        logger.warning("OAuth token failed: invalid client_id=%s", payload.client_id)
        _audit_auth_failure("OAuth: invalid credentials")
        from app.errors import UnauthorizedError
        raise UnauthorizedError("Invalid client_id or client_secret.")

    if key_record.get("name") != payload.client_id:
        logger.warning("OAuth token failed: client_id mismatch")
        _audit_auth_failure("OAuth: client_id mismatch")
        from app.errors import UnauthorizedError
        raise UnauthorizedError("Invalid client_id or client_secret.")

    if not key_record.get("is_active", False):
        logger.warning("OAuth token failed: revoked key")
        _audit_auth_failure("OAuth: revoked key")
        from app.errors import UnauthorizedError
        raise UnauthorizedError("Client credentials have been revoked.")

    settings = get_settings()
    scopes = key_record.get("scopes", [])
    now = int(time.time())
    expiry_minutes = settings.JWT_EXPIRY_MINUTES

    token_payload = {
        "sub": key_record.get("name"),
        "tenant_id": key_record.get("tenant_id"),
        "scopes": scopes,
        "iat": now,
        "exp": now + (expiry_minutes * 60),
    }

    token = jwt.encode(token_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    audit_record(
        action="oauth.token.issue",
        outcome="success",
        detail={"client_id": payload.client_id, "scopes": scopes},
    )
    logger.info("OAuth token issued: client=%s scopes=%s", payload.client_id, scopes)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expiry_minutes * 60,
        scope=" ".join(scopes),
    )


def _audit_auth_failure(reason: str) -> None:
    from app.audit import record as audit_record
    audit_record(
        action="auth.failure",
        outcome="denied",
        detail={"reason": reason},
    )
