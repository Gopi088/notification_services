"""
Notification repository (storage abstraction).

Backends:
- "sqlite"  (default/dev, backward compatible with the original database.py)
- "postgres" (production durable source of truth, per docs/03-DATA-MODEL.md)

The repository exposes CRUD + state-machine operations used by the API,
workers and webhook handlers. PostgreSQL is the durable source of truth; SQLite
is kept for local development and tests.
"""
import logging
import sqlite3
import uuid

try:
    import psycopg2
    from psycopg2 import errors as psycopg2_errors
except ImportError:  # pragma: no cover - psycopg2 not installed
    psycopg2 = None
    psycopg2_errors = None
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import get_settings

logger = logging.getLogger("storage")

# State machine states (per docs/03-DATA-MODEL.md)
QUEUED = "queued"
PROCESSING = "processing"
SUBMITTED = "submitted"
DELIVERED = "delivered"
FAILED = "failed"
RETRYING = "retrying"
DEAD_LETTERED = "dead_lettered"
CANCELLED = "cancelled"
READ = "read"
ACKNOWLEDGED = "acknowledged"
EXPIRED = "expired"

# Legal transitions: current -> {allowed next states}
TRANSITIONS: Dict[str, set] = {
    QUEUED: {PROCESSING, CANCELLED, EXPIRED},
    PROCESSING: {SUBMITTED, FAILED, RETRYING, DELIVERED, CANCELLED},
    SUBMITTED: {DELIVERED, FAILED, READ, EXPIRED},
    RETRYING: {PROCESSING, CANCELLED},
    FAILED: {RETRYING, DEAD_LETTERED},
    DEAD_LETTERED: {RETRYING},  # manual requeue
    DELIVERED: {READ, ACKNOWLEDGED},
    READ: {ACKNOWLEDGED},
    ACKNOWLEDGED: set(),
    CANCELLED: set(),
    EXPIRED: {CANCELLED},
}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id                   TEXT PRIMARY KEY,
    message_id           TEXT,
    group_id             TEXT,
    channel              TEXT NOT NULL,
    recipient            TEXT NOT NULL,
    message              TEXT NOT NULL,
    subject              TEXT,
    template_name        TEXT,
    template_language    TEXT,
    template_params      TEXT,
    content_hash         TEXT,
    status               TEXT NOT NULL,
    provider             TEXT,
    provider_message_id  TEXT,
    retry_count          INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 5,
    next_attempt_at      TEXT,
    idempotency_key      TEXT,
    request_id           TEXT,
    created_by           TEXT,
    reference            TEXT,
    last_error           TEXT,
    scheduled_at         TEXT,
    read_at              TEXT,
    acknowledged_at       TEXT,
    acknowledgement_type TEXT,
    acknowledgement_message TEXT,
    acknowledgement_source TEXT,
    parent_notification_id TEXT,
    resend_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id      TEXT NOT NULL,
    attempt              INTEGER NOT NULL,
    provider             TEXT,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    error_code           TEXT,
    error_message        TEXT,
    retryable            INTEGER,
    duration_ms          INTEGER,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id      TEXT NOT NULL,
    from_status          TEXT,
    to_status            TEXT,
    actor                TEXT,
    detail               TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                TEXT PRIMARY KEY,
    notification_id    TEXT NOT NULL,
    payload_hash       TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    provider             TEXT,
    provider_message_id  TEXT,
    status               TEXT,
    error_code           TEXT,
    error_message        TEXT,
    payload              TEXT,
    received_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id             TEXT NOT NULL,
    timestamp            TEXT NOT NULL,
    user_id              TEXT,
    action               TEXT NOT NULL,
    notification_id      TEXT,
    channel              TEXT,
    recipient_reference  TEXT,
    status               TEXT,
    provider             TEXT,
    ip_address           TEXT,
    request_id           TEXT,
    result               TEXT,
    failure_reason       TEXT,
    metadata             TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs (user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_notif ON audit_logs (notification_id);

CREATE TABLE IF NOT EXISTS inbound_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    channel              TEXT,
    from_number          TEXT,
    to_number            TEXT,
    text                 TEXT,
    provider_message_id  TEXT,
    raw                  TEXT,
    created_at           TEXT NOT NULL
);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id                   UUID PRIMARY KEY,
    message_id           UUID,
    group_id             UUID,
    channel              TEXT NOT NULL,
    recipient            TEXT NOT NULL,
    message              TEXT NOT NULL,
    subject              TEXT,
    template_name        TEXT,
    template_language    TEXT,
    template_params      JSONB,
    content_hash         TEXT,
    status               TEXT NOT NULL,
    provider             TEXT,
    provider_message_id  TEXT,
    retry_count          INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 5,
    next_attempt_at      TIMESTAMPTZ,
    idempotency_key      TEXT,
    request_id           TEXT,
    created_by           TEXT,
    reference            TEXT,
    last_error           TEXT,
    scheduled_at         TIMESTAMPTZ,
    read_at              TIMESTAMPTZ,
    acknowledged_at       TIMESTAMPTZ,
    acknowledgement_type TEXT,
    acknowledgement_message TEXT,
    acknowledgement_source TEXT,
    parent_notification_id TEXT,
    resend_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_status_next ON notifications (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_notifications_group ON notifications (group_id);
CREATE INDEX IF NOT EXISTS idx_notifications_provider_id ON notifications (provider_message_id);
CREATE INDEX IF NOT EXISTS idx_notifications_idem ON notifications (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS notification_attempts (
    id                   BIGSERIAL PRIMARY KEY,
    notification_id      UUID NOT NULL REFERENCES notifications(id),
    attempt              INTEGER NOT NULL,
    provider             TEXT,
    status               TEXT NOT NULL,
    provider_message_id  TEXT,
    error_code           TEXT,
    error_message        TEXT,
    retryable            BOOLEAN,
    duration_ms          INTEGER,
    created_at           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_notif ON notification_attempts (notification_id, attempt);

CREATE TABLE IF NOT EXISTS notification_events (
    id                   BIGSERIAL PRIMARY KEY,
    notification_id      UUID NOT NULL REFERENCES notifications(id),
    from_status          TEXT,
    to_status            TEXT,
    actor                TEXT,
    detail               JSONB,
    created_at           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_notif ON notification_events (notification_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                TEXT PRIMARY KEY,
    notification_id    UUID NOT NULL,
    payload_hash       TEXT,
    created_at         TIMESTAMPTZ NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id                   BIGSERIAL PRIMARY KEY,
    provider             TEXT,
    provider_message_id  TEXT,
    status               TEXT,
    error_code           TEXT,
    error_message        TEXT,
    payload              JSONB,
    received_at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_provider_id ON webhook_events (provider_message_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                   BIGSERIAL PRIMARY KEY,
    audit_id             TEXT NOT NULL,
    timestamp            TIMESTAMPTZ NOT NULL,
    user_id              TEXT,
    action               TEXT NOT NULL,
    notification_id      TEXT,
    channel              TEXT,
    recipient_reference  TEXT,
    status               TEXT,
    provider             TEXT,
    ip_address           TEXT,
    request_id           TEXT,
    result               TEXT,
    failure_reason       TEXT,
    metadata             JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs (user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_notif ON audit_logs (notification_id);

CREATE TABLE IF NOT EXISTS inbound_messages (
    id                   BIGSERIAL PRIMARY KEY,
    channel              TEXT,
    from_number          TEXT,
    to_number            TEXT,
    text                 TEXT,
    provider_message_id  TEXT,
    raw                  JSONB,
    created_at           TIMESTAMPTZ NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Unified storage interface over SQLite or PostgreSQL."""

    def __init__(self, backend: Optional[str] = None, url: Optional[str] = None):
        settings = get_settings()
        self.backend = (backend or settings.STORAGE_BACKEND or "sqlite").lower()
        self._url = url or settings.DATABASE_URL
        self._sqlite_path = settings.DATABASE_PATH
        self._conn = None
        self._pg_conn = None

    # ---- connection management ----
    def connect(self) -> None:
        if self.backend == "postgres":
            import psycopg2
            from psycopg2 import pool

            if not self._url:
                raise RuntimeError("DATABASE_URL is required when STORAGE_BACKEND=postgres")
            kwargs = {
                "minconn": get_settings().DB_POOL_MIN,
                "maxconn": get_settings().DB_POOL_MAX,
                "dsn": self._url,
            }
            try:
                self._pg_pool = pool.ThreadedConnectionPool(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.error("database connection failed backend=%s: %s", self.backend, exc)
                raise
            host = self._safe_host()
            logger.info(
                "database connected backend=%s host=%s", self.backend, host,
            )
        else:
            self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False, timeout=15.0)
            self._conn.row_factory = sqlite3.Row
            logger.info("storage connected (sqlite: %s)", self._sqlite_path)

    def _safe_host(self) -> str:
        """Return host[:port] from the DSN without exposing user/password."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self._url)
            netloc = parsed.netloc
            if "@" in netloc:
                netloc = netloc.split("@")[-1]
            return netloc or "unknown"
        except Exception:  # noqa: BLE001
            return "unknown"

    def init_schema(self) -> None:
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(PG_SCHEMA)
        else:
            with self._sqlite() as conn:
                conn.executescript(SQLITE_SCHEMA)
        logger.info("storage schema ready (%s)", self.backend)

    def close(self) -> None:
        if getattr(self, "_pg_pool", None) is not None:
            self._pg_pool.closeall()
        if self._conn is not None:
            self._conn.close()

    @contextmanager
    def _sqlite(self):
        conn = sqlite3.connect(self._sqlite_path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _pg(self):
        import time as _t

        for _ in range(5):
            try:
                conn = self._pg_pool.getconn()
            except Exception as exc:  # pool exhausted - wait and retry
                if "exhausted" in str(exc).lower():
                    _t.sleep(0.02)
                    continue
                raise
            break
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pg_pool.putconn(conn)

    # ---- helpers ----
    @staticmethod
    def _row_to_dict(row, cursor=None) -> Optional[Dict]:
        if row is None:
            return None
        if hasattr(row, "keys"):  # sqlite3.Row
            return {k: row[k] for k in row.keys()}
        # psycopg2 tuple + cursor.description
        cols = [d[0] for d in cursor.description] if cursor is not None else []
        d = dict(zip(cols, row)) if cols else dict(row)
        # Serialize PostgreSQL datetime/date values to ISO strings so API
        # response schemas (which expect str) validate cleanly.
        import datetime as _dt

        for k, v in d.items():
            if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
                d[k] = v.isoformat()
        return d

    @staticmethod
    def _pg_uuid_safe(value) -> Optional[str]:
        """Return the value only if it is a valid UUID (PostgreSQL UUID columns
        reject arbitrary strings). Used to keep status lookups from raising
        InvalidTextRepresentation for non-UUID ids."""
        import uuid as _uuid

        try:
            _uuid.UUID(str(value))
            return str(value)
        except (ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def _param(value) -> str:
        return "?" if get_settings().STORAGE_BACKEND != "postgres" else "%s"

    # ---- notifications ----
    def create_notification(self, *, message_id: str, channel: str, recipient: str,
                            message: str, status: str = QUEUED, group_id: Optional[str] = None,
                            reference: Optional[str] = None, subject: Optional[str] = None,
                            template_name: Optional[str] = None,
                            template_language: Optional[str] = None,
                            template_params: Optional[Dict] = None,
                            content_hash: Optional[str] = None,
                            idempotency_key: Optional[str] = None,
                            request_id: Optional[str] = None,
                            created_by: Optional[str] = None,
                            max_attempts: Optional[int] = None,
                            parent_notification_id: Optional[str] = None,
                            resend_count: int = 0) -> str:
        now = _now()
        nid = str(uuid.uuid4())
        params_json = None
        if template_params:
            import json
            params_json = json.dumps(template_params)
        max_a = max_attempts if max_attempts is not None else get_settings().MAX_ATTEMPTS
        if self.backend == "postgres":
            import json as _json

            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notifications
                       (id, message_id, group_id, channel, recipient, message, subject,
                        template_name, template_language, template_params, content_hash, status,
                        retry_count, max_attempts, idempotency_key, request_id, created_by,
                        reference, parent_notification_id, resend_count, created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (nid, message_id, group_id, channel, recipient, message, subject,
                     template_name, template_language, _json.dumps(template_params) if template_params else None,
                     content_hash,
                     status, 0, max_a, idempotency_key, request_id, created_by,
                     reference, parent_notification_id, resend_count, now, now),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """INSERT INTO notifications
                       (id, message_id, group_id, channel, recipient, message, subject,
                        template_name, template_language, template_params, content_hash, status,
                        retry_count, max_attempts, idempotency_key, request_id, created_by,
                        reference, parent_notification_id, resend_count, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (nid, message_id, group_id, channel, recipient, message, subject,
                     template_name, template_language, params_json, content_hash, status,
                     0, max_a, idempotency_key, request_id, created_by,
                     reference, parent_notification_id, resend_count, now, now),
                )
        logger.info("notification created id=%s channel=%s status=%s", nid, channel, status)
        return nid

    def get_notification(self, notification_id: str) -> Optional[Dict]:
        if self.backend == "postgres":
            safe = self._pg_uuid_safe(notification_id)
            if safe is None:
                return None
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM notifications WHERE id = %s", (safe,))
                row = cur.fetchone()
                return self._row_to_dict(row, cur)
        else:
            with self._sqlite() as conn:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE id = ?", (notification_id,)
                ).fetchone()
        return self._row_to_dict(row)

    def find_recent_by_content_hash(self, content_hash: str, window_minutes: int) -> Optional[Dict]:
        """Return the most recent notification matching a content fingerprint
        that was created within the window, or None.

        Used by window-based duplicate detection: the same user + channel +
        recipient + message/template sent again within the window is a
        duplicate; outside the window it is a new notification.
        """
        import datetime as _dt

        cutoff = (datetime.now(timezone.utc) - _dt.timedelta(minutes=window_minutes)).isoformat()
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notifications
                       WHERE content_hash = %s AND created_at >= %s
                       ORDER BY created_at DESC LIMIT 1""",
                    (content_hash, cutoff),
                )
                return self._row_to_dict(cur.fetchone(), cur)
        else:
            with self._sqlite() as conn:
                row = conn.execute(
                    """SELECT * FROM notifications
                       WHERE content_hash = ? AND created_at >= ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (content_hash, cutoff),
                ).fetchone()
                return self._row_to_dict(row)

    def get_notification_by_message_id(self, message_id: str) -> Optional[Dict]:
        if self.backend == "postgres":
            safe = self._pg_uuid_safe(message_id)
            if safe is None:
                return None
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM notifications WHERE message_id = %s", (safe,))
                row = cur.fetchone()
                return self._row_to_dict(row, cur)
        else:
            with self._sqlite() as conn:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE message_id = ?", (message_id,)
                ).fetchone()
        return self._row_to_dict(row)

    def get_by_provider_message_id(self, provider_message_id: str) -> Optional[Dict]:
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notifications WHERE provider_message_id = %s",
                    (provider_message_id,),
                )
                row = cur.fetchone()
                return self._row_to_dict(row, cur)
        else:
            with self._sqlite() as conn:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE provider_message_id = ?",
                    (provider_message_id,),
                ).fetchone()
        return self._row_to_dict(row)

    def get_by_parent_id(self, parent_id: str, limit: int = 50) -> List[Dict]:
        """Return notifications that are resends of the given parent (by id)."""
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notifications WHERE parent_notification_id = %s ORDER BY created_at DESC LIMIT %s",
                    (parent_id, limit),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE parent_notification_id = ? ORDER BY created_at DESC LIMIT ?",
                    (parent_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]

    def get_group(self, group_id: str) -> List[Dict]:
        if self.backend == "postgres":
            safe = self._pg_uuid_safe(group_id)
            if safe is None:
                return []
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notifications WHERE group_id = %s ORDER BY created_at ASC",
                    (safe,),
                )
                rows = cur.fetchall()
                return [self._row_to_dict(r, cur) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE group_id = ? ORDER BY created_at ASC",
                    (group_id,),
                ).fetchall()
                return [dict(r) for r in rows]

    def transition(self, notification_id: str, to_status: str, *, provider: Optional[str] = None,
                   provider_message_id: Optional[str] = None, error: Optional[str] = None,
                   actor: str = "system") -> Optional[Dict]:
        """Apply a guarded state transition. Returns the updated row or None."""
        row = self.get_notification(notification_id)
        if row is None:
            return None
        current = row["status"]
        allowed = TRANSITIONS.get(current, set())
        if to_status not in allowed:
            logger.warning(
                "invalid transition id=%s from=%s to=%s (allowed=%s)",
                notification_id, current, to_status, sorted(allowed),
            )
            return row
        now = _now()
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE notifications SET status=%s, provider=COALESCE(%s, provider),
                       provider_message_id=COALESCE(%s, provider_message_id),
                       last_error=%s, updated_at=%s,
                       read_at = CASE WHEN %s = 'read' THEN %s ELSE read_at END,
                       acknowledged_at = CASE WHEN %s = 'acknowledged' THEN %s ELSE acknowledged_at END,
                       retry_count = CASE WHEN %s = 'retrying' THEN retry_count + 1 ELSE retry_count END
                       WHERE id=%s""",
                    (to_status, provider, provider_message_id, error, now,
                     to_status, now, to_status, now, to_status, notification_id),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """UPDATE notifications SET status=?, provider=COALESCE(?, provider),
                       provider_message_id=COALESCE(?, provider_message_id),
                       last_error=?, updated_at=?,
                       read_at = CASE WHEN ? = 'read' THEN ? ELSE read_at END,
                       acknowledged_at = CASE WHEN ? = 'acknowledged' THEN ? ELSE acknowledged_at END,
                       retry_count = CASE WHEN ? = 'retrying' THEN retry_count + 1 ELSE retry_count END
                       WHERE id=?""",
                    (to_status, provider, provider_message_id, error, now,
                     to_status, now, to_status, now, to_status, notification_id),
                )
        self._insert_event(notification_id, current, to_status, actor, {"error": error})
        group_ref = row.get("group_id") if row else None
        logger.info(
            "notification status changed id=%s group_id=%s from=%s to=%s",
            notification_id, group_ref, current, to_status,
        )
        return self.get_notification(notification_id)

    def mark_read(self, notification_id: str, *, actor: str = "webhook") -> Optional[Dict]:
        """Transition submitted/delivered → read. Returns the updated row."""
        return self.transition(notification_id, READ, actor=actor)

    def mark_acknowledged(self, notification_id: str, *,
                          ack_type: str = "reply",
                          ack_message: Optional[str] = None,
                          ack_source: str = "inbound",
                          actor: str = "webhook") -> Optional[Dict]:
        """Transition delivered/read → acknowledged. Stores type/message/source."""
        now = _now()
        row = self.get_notification(notification_id)
        if row is None:
            return None
        current = row["status"]
        if current not in (DELIVERED, READ):
            logger.warning("invalid acknowledge from %s id=%s", current, notification_id)
            return row
        self.transition(notification_id, ACKNOWLEDGED, actor=actor)
        # Update acknowledgement metadata
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE notifications SET acknowledgement_type=%s, acknowledgement_message=%s,
                       acknowledgement_source=%s, updated_at=%s WHERE id=%s""",
                    (ack_type, ack_message, ack_source, now, notification_id),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """UPDATE notifications SET acknowledgement_type=?, acknowledgement_message=?,
                       acknowledgement_source=?, updated_at=? WHERE id=?""",
                    (ack_type, ack_message, ack_source, now, notification_id),
                )
        return self.get_notification(notification_id)

    def set_provider_info(self, notification_id: str, provider: str, provider_message_id: str) -> None:
        """Attach provider + provider_message_id to a notification (used by webhook
        reconciliation and tests) without changing its status."""
        now = _now()
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE notifications SET provider=%s, provider_message_id=%s, updated_at=%s
                       WHERE id=%s""",
                    (provider, provider_message_id, now, notification_id),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """UPDATE notifications SET provider=?, provider_message_id=?, updated_at=?
                       WHERE id=?""",
                    (provider, provider_message_id, now, notification_id),
                )

    def _insert_event(self, notification_id: str, from_status: Optional[str],
                      to_status: str, actor: str, detail: Optional[Dict]) -> None:
        now = _now()
        import json
        detail_json = json.dumps(detail) if detail else None
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notification_events
                       (notification_id, from_status, to_status, actor, detail, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (notification_id, from_status, to_status, actor, detail_json, now),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """INSERT INTO notification_events
                       (notification_id, from_status, to_status, actor, detail, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (notification_id, from_status, to_status, actor, detail_json, now),
                )

    def add_attempt(self, notification_id: str, attempt: int, status: str, *,
                    provider: Optional[str] = None, provider_message_id: Optional[str] = None,
                    error_code: Optional[str] = None, error_message: Optional[str] = None,
                    retryable: Optional[bool] = None, duration_ms: Optional[int] = None) -> None:
        now = _now()
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notification_attempts
                       (notification_id, attempt, provider, status, provider_message_id,
                        error_code, error_message, retryable, duration_ms, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (notification_id, attempt, provider, status, provider_message_id,
                     error_code, error_message, retryable, duration_ms, now),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """INSERT INTO notification_attempts
                       (notification_id, attempt, provider, status, provider_message_id,
                        error_code, error_message, retryable, duration_ms, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (notification_id, attempt, provider, status, provider_message_id,
                     error_code, error_message, int(retryable) if retryable is not None else None,
                     duration_ms, now),
                )

    def list_attempts(self, notification_id: str) -> List[Dict]:
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notification_attempts WHERE notification_id=%s ORDER BY attempt",
                    (notification_id,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(
                    "SELECT * FROM notification_attempts WHERE notification_id=? ORDER BY attempt",
                    (notification_id,),
                ).fetchall()
                return [dict(r) for r in rows]

    def find_idempotency_key_row(self, key: str) -> Optional[Dict]:
        """Return the raw idempotency_keys row for a key (durable dedup)."""
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM idempotency_keys WHERE key=%s AND expires_at >= %s",
                    (key, _now()),
                )
                row = cur.fetchone()
                return self._row_to_dict(row, cur)
        else:
            with self._sqlite() as conn:
                row = conn.execute(
                    "SELECT * FROM idempotency_keys WHERE key=? AND expires_at >= ?",
                    (key, _now()),
                ).fetchone()
                return self._row_to_dict(row)

    def find_by_idempotency_key(self, key: str) -> Optional[Dict]:
        """Backward-compatible: return the notification row for a key, or None."""
        row = self.find_idempotency_key_row(key)
        if row is None:
            return None
        nid = row.get("notification_id")
        return self.get_notification(nid) if nid else None

    def store_idempotency_key(self, key: str, notification_id: str, payload_hash: str,
                              ttl_seconds: Optional[int] = None) -> bool:
        now = _now()
        import datetime as _dt
        ttl = ttl_seconds if ttl_seconds is not None else get_settings().IDEMPOTENCY_TTL_SECONDS
        expires = (datetime.now(timezone.utc) + _dt.timedelta(seconds=ttl)).isoformat()
        try:
            if self.backend == "postgres":
                with self._pg() as conn, conn.cursor() as cur:
                    # Clear any EXPIRED row for this key so a new send can
                    # reuse the key once the duplicate window has passed. An
                    # unexpired row is left in place and makes the INSERT below
                    # conflict (concurrency mutex preserved).
                    cur.execute(
                        "DELETE FROM idempotency_keys WHERE key=%s AND expires_at < %s",
                        (key, now),
                    )
                    cur.execute(
                        """INSERT INTO idempotency_keys (key, notification_id, payload_hash, created_at, expires_at)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (key, notification_id, payload_hash, now, expires),
                    )
            else:
                with self._sqlite() as conn:
                    conn.execute(
                        "DELETE FROM idempotency_keys WHERE key=? AND expires_at < ?",
                        (key, now),
                    )
                    conn.execute(
                        """INSERT INTO idempotency_keys (key, notification_id, payload_hash, created_at, expires_at)
                           VALUES (?,?,?,?,?)""",
                        (key, notification_id, payload_hash, now, expires),
                    )
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate key -> conflict (SQLite)
        except Exception as exc:
            if psycopg2 and isinstance(exc, psycopg2.IntegrityError):
                return False  # duplicate key -> conflict (PostgreSQL)
            # Lock contention / connection error: propagate so the caller can
            # retry the claim (do not treat a transient lock error as a
            # duplicate).
            raise

    def due_notifications(self, limit: int = 100) -> List[Dict]:
        """Rows queued/retrying whose next_attempt_at has passed (reconciliation)."""
        now = _now()
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM notifications
                       WHERE status IN ('queued','retrying')
                         AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                       ORDER BY created_at ASC LIMIT %s""",
                    (now, limit),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(
                    """SELECT * FROM notifications
                       WHERE status IN ('queued','retrying')
                         AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                       ORDER BY created_at ASC LIMIT ?""",
                    (now, limit),
                ).fetchall()
                return [dict(r) for r in rows]

    def record_webhook_event(self, *, provider: str, provider_message_id: str, status: str,
                             error_code: Optional[str] = None,
                             error_message: Optional[str] = None,
                             payload: Optional[Dict] = None) -> None:
        now = _now()
        import json
        payload_json = json.dumps(payload) if payload else None
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO webhook_events
                       (provider, provider_message_id, status, error_code, error_message, payload, received_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (provider, provider_message_id, status, error_code, error_message,
                     payload_json, now),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """INSERT INTO webhook_events
                       (provider, provider_message_id, status, error_code, error_message, payload, received_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (provider, provider_message_id, status, error_code, error_message,
                     payload_json, now),
                )

    def record_audit(self, *, user_id: Optional[str], action: str,
                     notification_id: Optional[str] = None, channel: Optional[str] = None,
                     recipient_reference: Optional[str] = None, status: Optional[str] = None,
                     provider: Optional[str] = None, ip_address: Optional[str] = None,
                     request_id: Optional[str] = None, result: Optional[str] = None,
                     failure_reason: Optional[str] = None, metadata: Optional[Dict] = None) -> str:
        """Append an audit record (durable business/security log). Returns audit_id."""
        import json
        import uuid as _uuid

        audit_id = f"AUD_{_uuid.uuid4().hex[:12]}"
        now = _now()
        meta_json = json.dumps(metadata) if metadata else None
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_logs
                       (audit_id, timestamp, user_id, action, notification_id, channel,
                        recipient_reference, status, provider, ip_address, request_id,
                        result, failure_reason, metadata)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (audit_id, now, user_id, action, notification_id, channel,
                     recipient_reference, status, provider, ip_address, request_id,
                     result, failure_reason, meta_json),
                )
        else:
            with self._sqlite() as conn:
                conn.execute(
                    """INSERT INTO audit_logs
                       (audit_id, timestamp, user_id, action, notification_id, channel,
                        recipient_reference, status, provider, ip_address, request_id,
                        result, failure_reason, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (audit_id, now, user_id, action, notification_id, channel,
                     recipient_reference, status, provider, ip_address, request_id,
                     result, failure_reason, meta_json),
                )
        return audit_id

    def list_audit(self, limit: int = 50, user_id: Optional[str] = None,
                   action: Optional[str] = None) -> List[Dict]:
        """Return recent audit records, optionally filtered by user/action."""
        sql = "SELECT * FROM audit_logs"
        clauses = []
        params: List = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        if self.backend == "postgres":
            q = sql.replace("?", "%s")
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(q, params)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]

    def record_inbound_message(self, *, channel: str, from_number: str,
                               to_number: str, text: str,
                               provider_message_id: Optional[str] = None,
                               raw: Optional[Dict] = None) -> int:
        """Persist an inbound (reply) message. Returns the row id."""
        import json

        now = _now()
        raw_json = json.dumps(raw) if raw else None
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO inbound_messages
                       (channel, from_number, to_number, text, provider_message_id, raw, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (channel, from_number, to_number, text, provider_message_id, raw_json, now),
                )
                row = cur.fetchone()
                return row[0]
        else:
            with self._sqlite() as conn:
                cur = conn.execute(
                    """INSERT INTO inbound_messages
                       (channel, from_number, to_number, text, provider_message_id, raw, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (channel, from_number, to_number, text, provider_message_id, raw_json, now),
                )
                return cur.lastrowid

    def list_inbound(self, limit: int = 50) -> List[Dict]:
        """Return the most recent inbound messages."""
        if self.backend == "postgres":
            with self._pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM inbound_messages ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
        else:
            with self._sqlite() as conn:
                rows = conn.execute(
                    "SELECT * FROM inbound_messages ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]


_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
        _storage.connect()
        # SQLite is single-process / dev: safe to auto-init schema.
        # PostgreSQL schema is created by app/migrate.py (advisory-locked, idempotent)
        # to avoid the pg_type_typname_nsp_index race from concurrent containers.
        if _storage.backend != "postgres":
            _storage.init_schema()
    return _storage


def reset_storage() -> None:
    global _storage
    if _storage is not None:
        _storage.close()
        _storage = None
