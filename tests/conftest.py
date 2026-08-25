"""
Pytest configuration.

Test settings default to SQLite storage, MOCK_MODE=true, queue disabled, and a
temporary database file per session so tests never touch real providers,
production data, or real Redis/PostgreSQL unless explicitly overridden.
"""
import os
import tempfile

os.environ.setdefault("MOCK_MODE", "true")
os.environ.setdefault("STORAGE_BACKEND", "sqlite")
os.environ.setdefault("QUEUE_ENABLED", "false")
os.environ.setdefault("RATELIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "ERROR")

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("DATABASE_PATH", _tmp_db.name)


def pytest_configure(config):
    from app.config import get_settings

    get_settings.cache_clear()


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


import pytest  # noqa: E402


@pytest.fixture()
def client():
    """FastAPI TestClient with fresh storage."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.storage import get_storage, reset_storage

    get_settings.cache_clear()
    reset_storage()
    get_storage()
    with TestClient(app) as c:
        yield c
    reset_storage()


@pytest.fixture()
def storage():
    """Storage instance (SQLite test DB)."""
    from app.config import get_settings
    from app.storage import get_storage, reset_storage

    get_settings.cache_clear()
    reset_storage()
    s = get_storage()
    yield s
    reset_storage()