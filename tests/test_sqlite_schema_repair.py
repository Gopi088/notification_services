"""Regression test for automatic upgrades of old local SQLite databases."""
import sqlite3


def test_startup_repairs_missing_resend_columns(tmp_path, monkeypatch):
    from app.config import get_settings
    from app import migrate

    path = tmp_path / "legacy.db"
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_PATH", str(path))
    get_settings.cache_clear()
    migrate.up()

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("ALTER TABLE notifications DROP COLUMN parent_notification_id")
    conn.execute("ALTER TABLE notifications DROP COLUMN resend_count")
    conn.commit()
    conn.close()

    migrate.up()
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
    conn.close()
    assert {"parent_notification_id", "resend_count"} <= columns
    get_settings.cache_clear()
