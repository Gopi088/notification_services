"""
Shared fixtures for the pytest test suite.

Provides:
- A fresh temp database per test
- A TestClient wired to that database
- Workers disabled during tests to avoid async/sqlite conflicts
"""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    """Every test gets a fresh SQLite DB in a temp directory."""
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MOCK_MODE"] = "true"
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["WORKER_COUNT"] = "0"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.database import init_db
    init_db()

    yield db_path

    get_settings.cache_clear()


@pytest.fixture()
def client(_isolated_db):
    """FastAPI TestClient wired to the temp database, no background workers."""
    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import app
    with patch("app.workers.start_workers"), \
         patch("app.workers.stop_workers", return_value=None), \
         patch("app.database.reset_stale_processing", return_value=0), \
         patch("app.database.cleanup_expired_idempotency", return_value=0):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture()
def auth_client(_isolated_db):
    """TestClient with AUTH_ENABLED=true and a seeded API key."""
    os.environ["AUTH_ENABLED"] = "true"
    os.environ["WORKER_COUNT"] = "0"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.database import create_api_key, hash_api_key
    test_key = "test-api-key-12345"
    key_hash = hash_api_key(test_key)
    create_api_key(
        key_hash=key_hash,
        name="test-client",
        tenant_id="tenant-1",
        scopes=["send:write", "status:read", "admin:read"],
    )

    from app.main import app
    with patch("app.workers.start_workers"), \
         patch("app.workers.stop_workers", return_value=None), \
         patch("app.database.reset_stale_processing", return_value=0), \
         patch("app.database.cleanup_expired_idempotency", return_value=0):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, test_key


@pytest.fixture()
def readonly_auth_client(_isolated_db):
    """TestClient with an API key that only has read scopes."""
    os.environ["AUTH_ENABLED"] = "true"
    os.environ["WORKER_COUNT"] = "0"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.database import create_api_key, hash_api_key
    test_key = "readonly-key-aaaaa"
    key_hash = hash_api_key(test_key)
    create_api_key(
        key_hash=key_hash,
        name="readonly-client",
        tenant_id="tenant-2",
        scopes=["status:read"],
    )

    from app.main import app
    with patch("app.workers.start_workers"), \
         patch("app.workers.stop_workers", return_value=None), \
         patch("app.database.reset_stale_processing", return_value=0), \
         patch("app.database.cleanup_expired_idempotency", return_value=0):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, test_key


@pytest.fixture()
def rate_limited_auth_client(_isolated_db):
    """TestClient with an API key that has a 1 req/sec rate limit."""
    os.environ["AUTH_ENABLED"] = "true"
    os.environ["WORKER_COUNT"] = "0"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.database import create_api_key, hash_api_key
    test_key = "rate-limited-key-bbbb"
    key_hash = hash_api_key(test_key)
    create_api_key(
        key_hash=key_hash,
        name="rate-limited-client",
        tenant_id="tenant-3",
        scopes=["send:write"],
        rate_limit_per_second=1,
    )

    from app.main import app
    with patch("app.workers.start_workers"), \
         patch("app.workers.stop_workers", return_value=None), \
         patch("app.database.reset_stale_processing", return_value=0), \
         patch("app.database.cleanup_expired_idempotency", return_value=0):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, test_key
