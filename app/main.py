import logging
import os
import signal
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.audit import record as audit_record
from app.config import get_settings
from app.database import init_db
from app.errors import AppError, ErrorCode
from app.logging_config import setup_logging
from app.middleware import RequestIDMiddleware, RateLimitMiddleware
from app.orchestrator import get_message_summary
from app.queue import message_queue
from app.routers.notifications import router as legacy_router
from app.routers.v1 import router as v1_router
from app.routers.webhooks import router as webhook_router
from app.workers import start_workers, stop_workers

settings = get_settings()


def _resolve_log_level() -> str:
    """Determine log level with priority: CLI flag > env var > default.

    Priority order:
    1. --log-level CLI flag (highest)
    2. LOG_LEVEL environment variable
    3. Auto-detect: DEBUG if MOCK_MODE, else INFO
    """
    # Check CLI args for --log-level <level>
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--log-level" and i + 1 < len(args):
            return args[i + 1].upper()

    # Check env var
    env_level = os.environ.get("LOG_LEVEL", "").strip().upper()
    if env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        return env_level

    # Auto-detect based on mode
    return "DEBUG" if settings.MOCK_MODE else "INFO"


def _resolve_log_format() -> str:
    """Determine log format with priority: CLI flag > env var > default.

    Priority order:
    1. --log-format CLI flag (highest)
    2. LOG_FORMAT environment variable
    3. Auto-detect: plain if MOCK_MODE, else json
    """
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--log-format" and i + 1 < len(args):
            return args[i + 1].lower()

    env_format = os.environ.get("LOG_FORMAT", "").strip().lower()
    if env_format in ("json", "plain"):
        return env_format

    return "plain" if settings.MOCK_MODE else "json"


log_level = _resolve_log_level()
log_format = _resolve_log_format()
setup_logging(use_json=log_format == "json", log_level=log_level)
logger = logging.getLogger("app")

app = FastAPI(
    title=settings.APP_NAME,
    description="Versioned notification service: send WhatsApp/SMS/Email messages via /api/v1 and track delivery status.",
    version=__version__,
)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    default_per_second=settings.RATE_LIMIT_DEFAULT_PER_SECOND,
    window_seconds=60,
)


@app.on_event("startup")
def on_startup() -> None:
    from app.database import cleanup_expired_idempotency, reset_stale_processing
    init_db()
    stale_reset = reset_stale_processing(settings.WORKER_STALE_TIMEOUT_MINUTES)
    expired_cleaned = cleanup_expired_idempotency()
    start_workers(settings.WORKER_COUNT)
    logger.info(
        "Server started: mock_mode=%s version=%s workers=%d stale_reset=%d idempotency_cleaned=%d log_level=%s log_format=%s",
        settings.MOCK_MODE, __version__, settings.WORKER_COUNT,
        stale_reset, expired_cleaned,
        log_level, log_format,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.run_until_complete(stop_workers())
    except RuntimeError:
        pass
    logger.info("Server shutting down")


@app.exception_handler(AppError)
def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning(
        "AppError: %s %s code=%s message=%s",
        request.method, request.url.path, exc.code.value, exc.message,
        extra={"method": request.method, "path": str(request.url.path),
               "status_code": exc.http_status},
    )
    audit_record(
        action=f"http.{request.method.lower()}.{request.url.path}",
        outcome="error",
        detail={"code": exc.code.value, "message": exc.message},
    )
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", [])
        field = ".".join(str(l) for l in loc[1:]) if len(loc) > 1 else None
        msg = first.get("msg", "Validation error")
    else:
        field = None
        msg = "Validation error"

    logger.warning(
        "Validation error: %s %s field=%s message=%s",
        request.method, request.url.path, field, msg,
        extra={"method": request.method, "path": str(request.url.path),
               "status_code": 422},
    )
    audit_record(
        action=f"http.{request.method.lower()}.{request.url.path}",
        outcome="error",
        detail={"code": "validation_error", "message": msg, "field": field},
    )
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "message": msg,
                "field": field,
            },
        },
    )


@app.exception_handler(StarletteHTTPException)
def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        400: ErrorCode.VALIDATION_ERROR,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        405: ErrorCode.VALIDATION_ERROR,
        429: ErrorCode.RATE_LIMITED,
    }
    error_code = code_map.get(exc.status_code, ErrorCode.INTERNAL_ERROR)

    if exc.status_code >= 500:
        logger.error(
            "HTTP %d: %s %s detail=%s",
            exc.status_code, request.method, request.url.path, exc.detail,
            extra={"method": request.method, "path": str(request.url.path),
                   "status_code": exc.status_code},
        )
    else:
        logger.warning(
            "HTTP %d: %s %s detail=%s",
            exc.status_code, request.method, request.url.path, exc.detail,
            extra={"method": request.method, "path": str(request.url.path),
                   "status_code": exc.status_code},
        )

    audit_record(
        action=f"http.{request.method.lower()}.{request.url.path}",
        outcome="error",
        detail={"code": error_code.value, "message": str(exc.detail), "status": exc.status_code},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": error_code.value,
                "message": str(exc.detail),
                "field": None,
            },
        },
    )


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled error: %s %s",
        request.method, request.url.path,
        extra={"method": request.method, "path": str(request.url.path),
               "status_code": 500},
    )
    audit_record(
        action=f"http.{request.method.lower()}.{request.url.path}",
        outcome="error",
        detail={"code": "internal_error", "message": "Internal server error."},
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "Internal server error.",
                "field": None,
            },
        },
    )


app.include_router(v1_router)
app.include_router(legacy_router)
app.include_router(webhook_router)


@app.get("/health", summary="Health check")
def health() -> dict:
    from app.database import get_connection
    db_ok = True
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        db_ok = False
        logger.critical("Database connectivity check failed — service is degraded")

    return {
        "status": "ok" if db_ok else "degraded",
        "mock_mode": settings.MOCK_MODE,
        "version": __version__,
        "database": "ok" if db_ok else "error",
        "workers": settings.WORKER_COUNT,
        "auth_enabled": settings.AUTH_ENABLED,
    }


@app.get("/queue", summary="Queue status")
def queue_status() -> dict:
    return {
        "success": True,
        "queue": message_queue.stats,
    }
