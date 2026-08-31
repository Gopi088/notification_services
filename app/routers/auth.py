"""
JWT login endpoint.

POST /api/v1/auth/login  {client_id, client_secret}  ->  {access_token, ...}

Validates credentials against AUTH_CLIENT_ID / AUTH_CLIENT_SECRET (falls back
to AUTH_API_KEY) and returns a signed JWT. This route is intentionally NOT
protected by client auth - it is the entry point for obtaining a token.

Secrets (client_secret, tokens) are never logged.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.audit import record_audit
from app.auth import create_access_token, validate_client_credentials
from app.config import get_settings

logger = logging.getLogger("auth.login")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    client_id: str = Field(..., description="Client identifier")
    client_secret: str = Field(..., description="Client secret / credential")
    scopes: Optional[list] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Obtain a JWT access token",
    description=(
        "Authenticate a client and receive a Bearer JWT. Send it on every "
        "/api/v1/* request as `Authorization: Bearer <token>`."
    ),
    responses={401: {"description": "Invalid credentials"}},
)
def login(payload: LoginRequest, request: Request) -> LoginResponse:
    settings = get_settings()
    user_id = f"client_{payload.client_id}" if payload.client_id else "anonymous"

    if not settings.AUTH_ENABLED:
        # Dev mode: issue a token anyway so JWT flows work without config.
        token = create_access_token(payload.client_id, user_id)
        record_audit(user_id=user_id, action="login_succeeded",
                     result="success", metadata={"mode": "dev"})
        return LoginResponse(
            access_token=token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user_id=user_id,
        )

    if not validate_client_credentials(payload.client_id, payload.client_secret):
        logger.warning("login failed user_id=%s reason=invalid_credentials", user_id)
        record_audit(user_id=user_id, action="login_failed",
                     result="failure", failure_reason="invalid_credentials")
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            detail={"success": False, "error": {
                "code": "invalid_credentials",
                "message": "Invalid client_id or client_secret.",
                "field": None,
            }},
        )

    token = create_access_token(payload.client_id, user_id)
    record_audit(user_id=user_id, action="login_succeeded", result="success")
    logger.info("login succeeded user_id=%s", user_id)
    return LoginResponse(
        access_token=token,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_id=user_id,
    )
