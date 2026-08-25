"""
Root conftest: provides the `client` fixture for root-level test files
(test_webhooks.py, test_vonage_whatsapp.py) so they can run under pytest
as well as standalone.
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

import pytest  # noqa: E402


@pytest.fixture()
def client():
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


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass
