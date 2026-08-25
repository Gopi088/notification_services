"""
Database migrations.

Usage:
    python3 -m app.migrate up       # apply all migrations
    python3 -m app.migrate down     # rollback last migration
    python3 -m app.migrate status   # list applied migrations

Migrations run against the configured STORAGE_BACKEND. PostgreSQL uses a
schema_migrations tracking table; SQLite uses the same table for parity.

PostgreSQL advisory lock: all concurrent `up()` calls serialize on an advisory
lock (MIGRATION_LOCK_KEY = 747000001), so container startup races (api + workers
simultaneously initing the schema) are safe. Only one process creates/alters
tables; others wait and then see the migration already applied.
"""
import logging
import sys
import threading
from typing import List

from app.config import get_settings
from app.storage import PG_SCHEMA, SQLITE_SCHEMA, Storage

logger = logging.getLogger("migrations")

_MIGRATIONS: List[dict] = [
    {"id": "0001_initial_schema", "pg": PG_SCHEMA, "sqlite": SQLITE_SCHEMA},
]

# Advisory lock key for serializing concurrent PG migrations.
MIGRATION_LOCK_KEY = 747000001

# Same-process lock: serializes concurrent up() calls within one Python process
# (covers SQLite and the in-process background-task path). Cross-process
# PostgreSQL concurrency is handled by the advisory lock.
_MIGRATION_THREAD_LOCK = threading.Lock()


def _connect():
    storage = Storage()
    storage.connect()
    return storage


def _ensure_tracking(conn, cur=None, backend: str = "sqlite") -> None:
    if backend == "postgres":
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
        )
    else:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )


def _applied(conn, cur=None, backend: str = "sqlite") -> set:
    try:
        if backend == "postgres":
            cur.execute("SELECT id FROM schema_migrations")
            return {r[0] for r in cur.fetchall()}
        rows = conn.execute("SELECT id FROM schema_migrations").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def up() -> int:
    """Apply pending migrations. Returns the number of new migrations applied.

    Thread-safe: a same-process lock serializes calls (SQLite / in-process
    path); PostgreSQL additionally uses an advisory lock so concurrent
    containers never race on CREATE TABLE.
    """
    with _MIGRATION_THREAD_LOCK:
        return _up_locked()


def _up_locked() -> int:
    import datetime

    storage = _connect()
    backend = storage.backend
    count = 0
    if backend == "postgres":
        with storage._pg() as conn, conn.cursor() as cur:
            # Serialize concurrent migrations with a PostgreSQL advisory lock.
            # This prevents the pg_type_typname_nsp_index race when multiple
            # containers (api + workers) init the schema simultaneously.
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
            try:
                _ensure_tracking(conn, cur, backend)
                applied = _applied(conn, cur, backend)
                for m in _MIGRATIONS:
                    if m["id"] in applied:
                        continue
                    cur.execute(m["pg"])
                    cur.execute(
                        "INSERT INTO schema_migrations (id, applied_at) VALUES (%s, %s)",
                        (m["id"], datetime.datetime.now(datetime.timezone.utc).isoformat()),
                    )
                    logger.info("migration applied: %s", m["id"])
                    count += 1
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    else:
        with storage._sqlite() as conn:
            _ensure_tracking(conn, None, backend)
            applied = _applied(conn, None, backend)
            for m in _MIGRATIONS:
                if m["id"] in applied:
                    continue
                conn.executescript(m["sqlite"])
                conn.execute(
                    "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                    (m["id"], datetime.datetime.now(datetime.timezone.utc).isoformat()),
                )
                logger.info("migration applied: %s", m["id"])
                count += 1
    storage.close()
    return count


def status() -> None:
    storage = _connect()
    backend = storage.backend
    if backend == "postgres":
        with storage._pg() as conn, conn.cursor() as cur:
            _ensure_tracking(conn, cur, backend)
            applied = _applied(conn, cur, backend)
    else:
        with storage._sqlite() as conn:
            _ensure_tracking(conn, None, backend)
            applied = _applied(conn, None, backend)
    for m in _MIGRATIONS:
        mark = "APPLIED" if m["id"] in applied else "PENDING"
        print(f"  [{mark}] {m['id']}")
    storage.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    cmd = args[0] if args else "up"
    if cmd == "up":
        n = up()
        print(f"applied {n} migration(s)")
        return 0
    if cmd == "status":
        status()
        return 0
    if cmd == "down":
        print("down not supported yet (forward-only migrations); recreate is documented.")
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())