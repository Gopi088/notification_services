import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.database import init_db
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


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("app").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Versioned public API (recommended)
app.include_router(v1_router)

# Legacy unversioned routes (kept for backward compatibility)
app.include_router(legacy_router)


# Delivery-receipt webhook (Azure Event Grid)
app.include_router(webhook_router)


@app.get("/health", summary="Health check")
def health() -> dict:
    return {"status": "ok", "mock_mode": settings.MOCK_MODE, "version": __version__}
