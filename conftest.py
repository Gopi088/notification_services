"""
Root conftest: provides the `client` fixture for root-level test files
(test_webhooks.py, test_vonage_whatsapp.py) so they can run under pytest
as well as standalone.
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

import pytest  # noqa: E402

from tests.asgi_testclient import ASGITestClient, install_test_runtime_compatibility  # noqa: E402

install_test_runtime_compatibility()

# Test modules import this class after conftest is loaded.  Replacing it here
# confines the sandbox compatibility layer to pytest and preserves production
# FastAPI/Starlette behavior.
import fastapi.testclient  # noqa: E402

fastapi.testclient.TestClient = ASGITestClient


@pytest.fixture()
def client(tmp_path):
    import tempfile

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.storage import get_storage, reset_storage

    # Each test gets its own isolated SQLite file so records (audit, messages)
    # never leak between tests.
    db = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db
    get_settings.cache_clear()
    reset_storage()
    get_storage()
    with TestClient(app) as c:
        yield c
    reset_storage()
    os.environ["DATABASE_PATH"] = _tmp_db.name


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass
