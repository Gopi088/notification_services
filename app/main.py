import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import get_settings
from app.database import init_db
from app.errors import AppError, ErrorCode
from app.routers.notifications import router as legacy_router
from app.routers.v1 import router as v1_router
from app.routers.webhooks import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Versioned notification service: send WhatsApp/SMS/Email messages via /api/v1 and track delivery status.",
    version=__version__,
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.exception_handler(AppError)
def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
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
    logging.getLogger("app").exception("Unhandled error on %s", request.url.path)
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
    return {"status": "ok", "mock_mode": settings.MOCK_MODE, "version": __version__}
