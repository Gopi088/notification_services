"""
Pytest configuration.

Test settings default to SQLite storage, MOCK_MODE=true, queue disabled, and a
temporary database file per session so tests never touch real providers,
production data, or real Redis/PostgreSQL unless explicitly overridden.
"""
import os
import tempfile

# Force the test environment so `.env` can never leak real CockroachDB
# credentials / STORAGE_BACKEND into the pytest run. Direct assignment
# (not setdefault) guarantees the suite always uses SQLite + local Postgres
# unless an individual test overrides via monkeypatch.
os.environ["MOCK_MODE"] = "true"
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ["DATABASE_BACKEND"] = "postgres"
os.environ["DATABASE_URL"] = ""
os.environ["COCKROACHDB_CA_CERT_PATH"] = ""
os.environ["QUEUE_ENABLED"] = "false"
os.environ["RATELIMIT_ENABLED"] = "false"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["AUTH_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_PATH"] = _tmp_db.name


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