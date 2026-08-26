"""
Application entry point.

Starts FastAPI, initializes the storage layer (SQLite or PostgreSQL), wires
all routers, and exposes liveness/readiness/health endpoints.
"""
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.auth import require_api_key
from app.logging_config import configure_logging
from app.routers.notifications import router as legacy_router
from app.routers.v1 import router as v1_router
from app.routers.webhooks import router as webhook_router
from app.routers.inbound import router as inbound_router
from app.storage import get_storage

configure_logging()
logger = logging.getLogger("app")

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Versioned notification service: send WhatsApp/SMS/Email messages via /api/v1 and track delivery status.",
    version=__version__,
)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("application startup version=%s mock_mode=%s storage=%s queue=%s",
                __version__, settings.MOCK_MODE, settings.STORAGE_BACKEND, settings.QUEUE_ENABLED)
    # Initialize durable storage (source of truth) + schema.
    get_storage()
    # Run migrations on every backend. This upgrades an existing local SQLite
    # database before requests use columns added by newer application versions.
    from app.migrate import up as run_migrations

    n = run_migrations()
    logger.info("migrations applied this startup: %s", n)
    # Keep legacy SQLite table available for backward-compatible tooling.
    try:
        from app.database import init_db

        init_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy sqlite init skipped: %s", exc)


@app.on_event("shutdown")
def on_shutdown() -> None:
    logger.info("application shutdown starting")
    from app.storage import reset_storage

    reset_storage()
    logger.info("application shutdown complete")


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    """Log malformed requests without recording submitted values or secrets."""
    logger.error(
        "request validation failed method=%s path=%s status=422 error_count=%d",
        request.method, request.url.path, len(exc.errors()),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Log handled client errors without logging bodies, headers, or secrets."""
    level = logger.error if exc.status_code >= 400 else logger.info
    level(
        "request rejected method=%s path=%s status=%d",
        request.method, request.url.path, exc.status_code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map typed application errors to their HTTP status + uniform envelope;
    anything else becomes a generic 500 internal_error (no secrets leaked)."""
    from app.errors import AppError, classify_provider_error

    if isinstance(exc, AppError):
        logger.warning(
            "request error path=%s code=%s message=%s",
            request.url.path, exc.code, exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": exc.to_dict()},
        )

    # Provider exceptions raised outside the worker (e.g. synchronous path)
    # are classified into typed errors.
    classified = classify_provider_error(exc)
    if isinstance(classified, AppError) and classified.code != "internal_error":
        logger.warning(
            "request provider error path=%s code=%s message=%s",
            request.url.path, classified.code, classified.message,
        )
        return JSONResponse(
            status_code=classified.status_code,
            content={"success": False, "error": classified.to_dict()},
        )

    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": {"code": "internal_error",
                                             "message": "Internal server error."}},
    )


# Versioned public API (recommended)
app.include_router(v1_router, dependencies=[Depends(require_api_key)])

# Legacy unversioned routes (kept for backward compatibility)
# Legacy `/send` and `/status` stay public for local CLI and Postman use.
app.include_router(legacy_router)


# Delivery-receipt webhook (Azure Event Grid)
app.include_router(webhook_router)

# Inbound (reply) webhook - recipients replying to notifications
app.include_router(inbound_router)


@app.get("/health", summary="Liveness: process is up")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "mock_mode": s.MOCK_MODE, "version": __version__,
            "auth_enabled": s.AUTH_ENABLED}


@app.get("/api/v1/health/liveness", summary="Liveness check")
def liveness() -> dict:
    return {"status": "ok", "service": settings.APP_NAME, "version": __version__}


@app.get("/api/v1/health/readiness", summary="Readiness check")
def readiness() -> JSONResponse:
    """Ready when the durable storage layer is reachable.

    Queue readiness is included when QUEUE_ENABLED=true. Fails open (ready)
    on MOCK_MODE so local/dev flows work without real dependencies.
    """
    settings = get_settings()
    checks = {"storage": False, "queue": None}
    ok = True
    try:
        from app.storage import get_storage

        # Probe storage by checking if a simple query works (connectivity check).
        s = get_storage()
        if s.backend == "postgres":
            with s._pg() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
        else:
            s.get_notification("readiness-probe")
        checks["storage"] = True
    except Exception as exc:  # noqa: BLE001
        logger.error("readiness storage check failed: %s", exc)
        ok = False

    if settings.QUEUE_ENABLED:
        try:
            from app import queue as q

            q.queue_length("sms")
            checks["queue"] = True
        except Exception as exc:  # noqa: BLE001
            logger.error("readiness queue check failed: %s", exc)
            checks["queue"] = False
            ok = False

    if settings.MOCK_MODE and not ok:
        logger.warning("readiness degraded in mock mode: %s", checks)
        ok = True

    body = {"status": "ok" if ok else "unavailable", "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=body)
