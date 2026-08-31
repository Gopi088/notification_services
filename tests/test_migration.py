"""Tests for the migration/init race-condition fix.

Covers:
- first schema initialization
- repeated initialization (idempotent)
- concurrent initialization (threads)
- application/worker restart (re-run up())
- migration failure handling
- migration success
- existing database with existing tables (no re-apply)
- existing notification data preserved
"""
import os
import sqlite3
import threading

import pytest


@pytest.fixture()
def mig_settings(tmp_path, monkeypatch):
    """Point the app at an isolated SQLite file for migration tests."""
    from app.config import get_settings

    db = str(tmp_path / "mig_test.db")
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_PATH", db)
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_first_schema_initialization(mig_settings):
    from app import migrate

    n = migrate.up()
    assert n >= 1
    # tables exist
    conn = sqlite3.connect(mig_settings)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('notifications','notification_attempts','audit_logs','schema_migrations')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"notifications", "notification_attempts", "audit_logs", "schema_migrations"} <= names
    conn.close()


def test_repeated_schema_initialization_idempotent(mig_settings):
    from app import migrate

    n1 = migrate.up()
    n2 = migrate.up()
    n3 = migrate.up()
    assert n1 >= 1
    assert n2 == 0
    assert n3 == 0


def test_concurrent_schema_initialization(mig_settings):
    """Many threads call up() at once; exactly one applies, none crash."""
    from app import migrate

    results = []
    errors = []

    def worker():
        try:
            results.append(migrate.up())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent migration raised: {errors}"
    # Exactly one thread applied 1 migration; the rest saw 0.
    assert sum(1 for r in results if r >= 1) == 1
    assert all(r == 0 or r >= 1 for r in results)


def test_restart_re_runs_idempotent(mig_settings):
    """Simulate app restart: up() again returns 0 and preserves data."""
    from app import migrate
    from app.storage import get_storage

    migrate.up()
    storage = get_storage()
    nid = storage.create_notification(
        message_id="restart-1", channel="sms", recipient="+919887270348",
        message="keep me", status="queued",
    )
    # "restart": fresh migrate run
    n = migrate.up()
    assert n == 0
    row = storage.get_notification(nid)
    assert row is not None
    assert row["message"] == "keep me"


def test_existing_data_preserved_after_migration(mig_settings):
    from app import migrate
    from app.storage import get_storage

    migrate.up()
    storage = get_storage()
    storage.create_notification(
        message_id="data-1", channel="sms", recipient="+919887270348",
        message="existing", status="queued",
    )
    migrate.up()  # re-run
    row = storage.get_notification_by_message_id("data-1")
    assert row is not None
    assert row["status"] == "queued"


def test_existing_tables_no_reapply(mig_settings):
    """A DB that already has tables + a recorded migration is a no-op."""
    import sqlite3

    from app import migrate

    migrate.up()  # first apply records schema_migrations row
    conn = sqlite3.connect(mig_settings)
    applied = migrate._applied(conn, None, "sqlite")
    conn.close()
    assert "0001_initial_schema" in applied
    n = migrate.up()
    assert n == 0


def test_migration_failure_propagates(mig_settings):
    """If migration connect/DDL fails, the error propagates (not swallowed)."""
    import os

    from app.config import get_settings

    from app import migrate

    # Point at an invalid DB path so connect() raises -> up() propagates.
    os.environ["DATABASE_PATH"] = "/nonexistent-dir-xyz/notifications.db"
    get_settings.cache_clear()
    with pytest.raises(Exception):
        migrate.up()
    os.environ["DATABASE_PATH"] = mig_settings
    get_settings.cache_clear()


def test_migration_adds_content_hash_to_existing_db(mig_settings):
    """An older DB without content_hash gets the column via migration."""
    import sqlite3

    from app import migrate

    # Simulate an old database: full schema minus the content_hash column.
    conn = sqlite3.connect(mig_settings)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE notifications (
            id TEXT PRIMARY KEY, message_id TEXT, group_id TEXT, channel TEXT NOT NULL,
            recipient TEXT NOT NULL, message TEXT NOT NULL, subject TEXT,
            template_name TEXT, template_language TEXT, template_params TEXT,
            status TEXT NOT NULL, provider TEXT, provider_message_id TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at TEXT, idempotency_key TEXT, request_id TEXT, created_by TEXT,
            reference TEXT, last_error TEXT, scheduled_at TEXT, read_at TEXT,
            acknowledged_at TEXT, acknowledgement_type TEXT,
            acknowledgement_message TEXT, acknowledgement_source TEXT,
            parent_notification_id TEXT, resend_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    # Mark the first three migrations applied so only 0004 (content_hash) runs.
    for mid in ("0001_initial_schema", "0002_add_acknowledgement_columns",
                "0003_add_parent_resend_columns"):
        conn.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                     (mid, "2026-01-01T00:00:00+00:00"))
    conn.commit()
    conn.close()

    n = migrate.up()
    assert n == 2  # 0004 (content_hash) + 0005 (delivered_at) applied

    conn = sqlite3.connect(mig_settings)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    assert "content_hash" in columns
    assert "delivered_at" in columns
    conn.close()


def test_migration_success_records_applied(mig_settings):
    from app import migrate

    migrate.up()
    conn = sqlite3.connect(mig_settings)
    row = conn.execute(
        "SELECT id FROM schema_migrations WHERE id='0001_initial_schema'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_migrate_status(mig_settings, capsys):
    from app import migrate

    migrate.up()
    migrate.status()
    out = capsys.readouterr().out
    assert "0001_initial_schema" in out
    assert "APPLIED" in out


def test_get_storage_does_not_autocreate_postgres_schema(monkeypatch):
    """get_storage() must NOT init PG schema (migration owns it)."""
    from unittest.mock import patch

    from app.config import get_settings

    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://postgres:testpass@localhost:5434/notifications")
    get_settings.cache_clear()
    from app.storage import Storage, get_storage, reset_storage

    reset_storage()
    with patch.object(Storage, "init_schema") as mock_init:
        get_storage()
        mock_init.assert_not_called()  # PG schema not auto-created here
    reset_storage()
    get_settings.cache_clear()
