"""
Pytest configuration.

Test settings default to SQLite storage, MOCK_MODE=true, queue disabled, and a
temporary database file per session so tests never touch real providers,
production data, or real Redis/PostgreSQL unless explicitly overridden.
"""
import os
import tempfile

# Force the test environment so `.env` can never leak production settings
# into the pytest run. Direct assignment (not setdefault) guarantees the suite
# always uses SQLite unless an individual test overrides via monkeypatch.
os.environ["MOCK_MODE"] = "true"
os.environ["STORAGE_BACKEND"] = "sqlite"
os.environ["DATABASE_URL"] = ""
os.environ["QUEUE_ENABLED"] = "false"
os.environ["RATELIMIT_ENABLED"] = "false"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["AUTH_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"
# Twilio must never be picked up from `.env` unless a test opts in explicitly.
os.environ["TWILIO_ACCOUNT_SID"] = ""
os.environ["TWILIO_AUTH_TOKEN"] = ""
os.environ["TWILIO_FROM"] = ""
os.environ["TWILIO_WHATSAPP_FROM"] = ""

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
def client(tmp_path):
    """FastAPI TestClient with a fresh isolated DB."""
    import os

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.orchestrator import wait_for_mock_deliveries
    from app.storage import get_storage, reset_storage

    db = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db
    get_settings.cache_clear()
    wait_for_mock_deliveries()
    reset_storage()
    get_storage()
    with TestClient(app) as c:
        yield c
    wait_for_mock_deliveries()
    reset_storage()


@pytest.fixture()
def storage(tmp_path):
    """Storage instance (isolated SQLite test DB)."""
    import os

    from app.config import get_settings
    from app.orchestrator import wait_for_mock_deliveries
    from app.storage import get_storage, reset_storage

    db = str(tmp_path / "storage.db")
    os.environ["DATABASE_PATH"] = db
    get_settings.cache_clear()
    wait_for_mock_deliveries()
    reset_storage()
    s = get_storage()
    yield s
    wait_for_mock_deliveries()
    reset_storage()
