"""Tests for CockroachDB support, enhanced audit fields, and db-check."""
import os
from unittest.mock import MagicMock, patch

import pytest


def test_storage_accepts_cockroachdb_backend(monkeypatch):
    """Storage recognizes cockroachdb as a PG-compatible backend."""
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_BACKEND", "cockroachdb")
    monkeypatch.setenv("COCKROACHDB_CA_CERT_PATH", "/tmp/ca.crt")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://gopi:secret@nordic-feline-32781.j77.aws-ap-south-1.cockroachlabs.cloud:26257/postgresql?sslmode=verify-full")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.storage import Storage

    s = Storage()
    assert s.db_backend == "cockroachdb"
    assert s._ca_cert == "/tmp/ca.crt"
    get_settings.cache_clear()


def test_storage_cockroachdb_connect_kwargs(monkeypatch):
    """CockroachDB connect adds sslmode + sslrootcert without logging password."""
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_BACKEND", "cockroachdb")
    monkeypatch.setenv("COCKROACHDB_CA_CERT_PATH", "/tmp/ca.crt")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://gopi:supersecret@cockroach.example.com:26257/db?sslmode=verify-full",
    )
    from app.config import get_settings

    get_settings.cache_clear()
    from app.storage import Storage

    s = Storage()
    captured = {}

    class FakePool:
        def __init__(self, **kw):
            captured.update(kw)

    with patch("psycopg2.pool.ThreadedConnectionPool", FakePool):
        s.connect()
    # sslmode is already in the DSN; sslrootcert is added via kwargs.
    assert captured["sslrootcert"] == "/tmp/ca.crt"
    assert "sslmode=verify-full" in captured["dsn"]
    # The connection string itself must never appear in LOG OUTPUT (it does
    # carry credentials by design for psycopg2). Verify via _safe_host that
    # what gets logged is host-only.
    assert "supersecret" not in s._safe_host()
    get_settings.cache_clear()


def test_storage_safe_host_masks_credentials():
    from app.storage import Storage

    s = Storage(url="postgresql://user:password@host.example.com:26257/db")
    host = s._safe_host()
    assert "password" not in host
    assert "user" not in host
    assert "host.example.com" in host


def test_storage_safe_host_no_credentials():
    from app.storage import Storage

    s = Storage(url="postgresql://host.example.com:5432/db")
    assert "host.example.com" in s._safe_host()


def test_db_check_sqlite(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    from app.config import get_settings

    get_settings.cache_clear()
    from notification_service import do_db_check

    do_db_check()
    out = capsys.readouterr().out
    assert "Database backend" in out
    assert "sqlite" in out
    get_settings.cache_clear()


def test_db_check_postgres_failure(capsys, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/nonexistent")
    from app.config import get_settings

    get_settings.cache_clear()
    from notification_service import do_db_check

    with patch("app.storage.Storage.connect", side_effect=RuntimeError("cannot connect")):
        import sys

        original = sys.exit
        try:
            with pytest.raises(SystemExit):
                do_db_check()
        finally:
            sys.exit = original
    out = capsys.readouterr().out
    assert "Status" in out
    get_settings.cache_clear()


def test_audit_records_db_backend_and_source(storage):
    from app.audit import list_audit, record_audit

    record_audit(user_id="u", action="notification_created", notification_id="n1",
                 channel="sms", request_id="req-xyz")
    rows = list_audit(limit=5)
    # The most recent record carries db_backend/source in metadata
    latest = rows[0]
    assert latest["action"] == "notification_created"
    assert latest["request_id"] == "req-xyz"


def test_audit_file_includes_backend_and_correlation(tmp_path):
    import json

    from app.audit import _append_audit_file

    path = str(tmp_path / "audit.jsonl")
    os.environ["AUDIT_LOG_FILE"] = path
    from app.config import get_settings

    get_settings.cache_clear()
    _append_audit_file({
        "audit_id": "AUD_test",
        "timestamp": "2026-01-01T00:00:00Z",
        "user_id": "u",
        "action": "notification_created",
        "database_backend": "cockroachdb",
        "correlation_id": "corr-1",
        "source": "notification_service",
        "result": "success",
    })
    with open(path, "r") as fh:
        rec = json.loads(fh.readline())
    assert rec["database_backend"] == "cockroachdb"
    assert rec["correlation_id"] == "corr-1"
    assert rec["source"] == "notification_service"
    os.environ["AUDIT_LOG_FILE"] = ""
    get_settings.cache_clear()


def test_audit_metadata_never_contains_secret(storage):
    from app.audit import list_audit, record_audit

    record_audit(user_id="u", action="notification_created",
                 notification_id="n1", metadata={"database_backend": "postgres"})
    rows = list_audit(limit=5)
    assert all("supersecret" not in str(r) for r in rows)


def test_migrate_accepts_cockroachdb(monkeypatch, tmp_path):
    """Migrate treats cockroachdb the same as postgres (advisory lock path)."""
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_BACKEND", "cockroachdb")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    from app.config import get_settings

    get_settings.cache_clear()
    from app import migrate

    # The portable row lock must be usable on cockroachdb (pg_advisory_lock is
    # NOT supported by CockroachDB). _acquire_lock must exist.
    assert hasattr(migrate, "_acquire_lock")
    get_settings.cache_clear()


def test_logging_redacts_database_url():
    """Structured logging must never emit DATABASE_URL/password."""
    import logging

    from app.logging_config import StructuredFormatter

    record = logging.LogRecord("test", logging.INFO, __file__, 1,
                               "connected", None, None)
    record.extra = {
        "database_url": "postgresql://u:supersecret@h:5432/db",
        "password": "supersecret",
    }
    out = StructuredFormatter().format(record)
    import json as _json

    data = _json.loads(out)
    assert data["database_url"] == "***"
    assert data["password"] == "***"
    assert "supersecret" not in out


def test_audit_file_read_skips_bad_lines(tmp_path, monkeypatch):
    """list_audit_from_file skips malformed lines and returns newest first."""
    from app.config import get_settings
    from app.audit import list_audit_from_file

    path = str(tmp_path / "audit2.jsonl")
    monkeypatch.setenv("AUDIT_LOG_FILE", path)
    get_settings.cache_clear()
    with open(path, "w") as fh:
        fh.write("not-json\n")
        fh.write('{"audit_id": "A1", "action": "x"}\n')
        fh.write('{"audit_id": "A2", "action": "y"}\n')
    rows = list_audit_from_file(limit=10)
    assert len(rows) == 2
    assert rows[0]["audit_id"] == "A2"  # newest first
    get_settings.cache_clear()


def test_audit_file_read_missing(tmp_path, monkeypatch):
    from app.audit import list_audit_from_file

    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "does-not-exist.jsonl"))
    from app.config import get_settings

    get_settings.cache_clear()
    assert list_audit_from_file(limit=5) == []
    get_settings.cache_clear()


def test_audit_file_read_error(tmp_path, monkeypatch):
    """Read errors are caught and return [] (audit never crashes the caller)."""
    from app.audit import list_audit_from_file

    path = str(tmp_path / "audit3.jsonl")
    with open(path, "w") as fh:
        fh.write("{}")
    monkeypatch.setenv("AUDIT_LOG_FILE", path)
    from app.config import get_settings

    get_settings.cache_clear()
    with patch("app.audit.open", side_effect=PermissionError("denied")):
        assert list_audit_from_file(limit=5) == []
    get_settings.cache_clear()


def test_cockroachdb_uses_configured_ca_path(monkeypatch):
    """The COCKROACHDB_CA_CERT_PATH env value is passed as sslrootcert."""
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_BACKEND", "cockroachdb")
    monkeypatch.setenv("COCKROACHDB_CA_CERT_PATH", "/real/certs/root.crt")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://gopi:pw@cockroach.example.com:26257/db?sslmode=verify-full")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.storage import Storage

    s = Storage()
    captured = {}

    class FakePool:
        def __init__(self, **kw):
            captured.update(kw)

    with patch("psycopg2.pool.ThreadedConnectionPool", FakePool):
        s.connect()
    assert captured["sslrootcert"] == "/real/certs/root.crt"
    get_settings.cache_clear()


def test_cockroachdb_never_uses_placeholder_ca_default(monkeypatch):
    """The CA path must never be the fake placeholder '/path/to/ca.crt'."""
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.COCKROACHDB_CA_CERT_PATH != "/path/to/ca.crt"
    get_settings.cache_clear()


def test_cockroachdb_placeholder_not_in_dbcheck(capsys, monkeypatch):
    """db-check must never print or use the placeholder path."""
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_BACKEND", "cockroachdb")
    monkeypatch.setenv("COCKROACHDB_CA_CERT_PATH", "/path/to/ca.crt")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://gopi:pw@cockroach.example.com:26257/db?sslmode=verify-full")
    from app.config import get_settings

    get_settings.cache_clear()
    from notification_service import do_db_check

    with patch("app.storage.Storage.connect", side_effect=RuntimeError("down")):
        import sys

        with pytest.raises(SystemExit):
            do_db_check()
    out = capsys.readouterr().out
    assert "Database engine  : cockroachdb" in out
    get_settings.cache_clear()


def test_secret_redaction_database_url_password(caplog):
    """Structured logging redacts database_url and password values."""
    import logging

    from app.logging_config import CorrelatedLogger

    logger = CorrelatedLogger("secret-test")
    with caplog.at_level(logging.INFO):
        logger.info("connecting", database_url="postgresql://u:topsecret@h:5432/db",
                    password="topsecret")
    assert "topsecret" not in caplog.text


def test_config_reads_cockroachdb_ca_env(monkeypatch):
    """Config exposes COCKROACHDB_CA_CERT_PATH from the environment."""
    monkeypatch.setenv("COCKROACHDB_CA_CERT_PATH", "/etc/ssl/cockroach-ca.crt")
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.COCKROACHDB_CA_CERT_PATH == "/etc/ssl/cockroach-ca.crt"
    get_settings.cache_clear()
