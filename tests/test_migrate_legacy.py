"""Tests for migrate CLI, worker_runner entry, and legacy database module."""
import pytest


def test_migrate_status_and_up(tmp_path, monkeypatch):
    import sys

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(get_settings(), "DATABASE_PATH", str(tmp_path / "mig.db"))
    from app import migrate

    # up on a fresh db
    n = migrate.up()
    assert n >= 1
    # second run -> 0 new
    n2 = migrate.up()
    assert n2 == 0


def test_migrate_main_unknown_command(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["app.migrate", "bogus"])
    from app import migrate

    assert migrate.main() == 2


def test_migrate_main_up(monkeypatch, capsys, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "DATABASE_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr("sys.argv", ["app.migrate", "up"])
    from app import migrate

    assert migrate.main() == 0


def test_worker_runner_usage(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["worker_runner"])
    from app.worker_runner import main

    assert main() == 2


def test_worker_runner_channel_start(monkeypatch):
    """run_worker starts and stops on signal via stop event - just verify no crash
    when storage is ready (we exercise the import + guard path)."""
    monkeypatch.setattr("sys.argv", ["worker_runner", "whatsapp", "--worker-id", "t1"])
    import app.worker_runner as wr
    import app.worker as worker_mod

    # monkeypatch run_worker to avoid infinite loop
    calls = []

    def fake_run(channel, worker_id=None):
        calls.append((channel, worker_id))

    monkeypatch.setattr(worker_mod, "run_worker", fake_run)
    assert wr.main() == 0
    assert calls == [("whatsapp", "t1")]


def test_worker_runner_retry(monkeypatch):
    import app.worker_runner as wr
    import app.worker as worker_mod

    def fake_retry():
        pass

    monkeypatch.setattr(worker_mod, "run_retry_worker", fake_retry)
    monkeypatch.setattr("sys.argv", ["worker_runner", "--retry"])
    assert wr.main() == 0


def test_legacy_database_crud(tmp_path, monkeypatch):
    """Legacy app.database module (kept for backward compat tooling)."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "DATABASE_PATH", str(tmp_path / "legacy.db"))
    from app import database

    database.init_db()
    database.create_message("m1", "sms", "+919887270348", "hi", "queued")
    row = database.get_message("m1")
    assert row is not None
    assert row["status"] == "queued"

    database.update_status("m1", "sent", provider="vonage_sms", provider_message_id="pm-1")
    row = database.get_message("m1")
    assert row["status"] == "sent"
    assert row["provider"] == "vonage_sms"

    database.update_status_by_provider_id("pm-1", "delivered")
    row = database.get_message("m1")
    assert row["status"] == "delivered"

    assert database.get_message("missing") is None

    database.create_message("m2", "whatsapp", "+919887270348", "x", "queued", group_id="g1")
    rows = database.get_group("g1")
    assert len(rows) == 1

    listed = database.list_messages(channel="sms")
    assert len(listed) >= 1
