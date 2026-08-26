"""
Lightweight SQLite persistence layer for tracking message/delivery state.

No ORM is used on purpose -- this service has one table and a handful of
queries, so raw sqlite3 keeps things simple and dependency-free.

A `group_id` links every channel attempt for one logical send, so a single
request fanning out to whatsapp+sms+email shares a group and can be queried
together via GET /api/v1/notifications/{group_id}/status.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    group_id            TEXT,
    channel              TEXT NOT NULL,
    contact              TEXT NOT NULL,
    message               TEXT NOT NULL,
    status                TEXT NOT NULL,
    provider              TEXT,
    provider_message_id   TEXT,
    error                 TEXT,
    reference             TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
"""

_API_KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash                TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    tenant_id               TEXT NOT NULL,
    scopes                  TEXT NOT NULL DEFAULT '[]',
    rate_limit_per_second   INTEGER,
    is_active               INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL,
    expires_at              TEXT
);
"""

_IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'processing',
    response_body   TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""

_MIGRATIONS = [
    # 0.1 -> 0.2: add group_id for multi-channel sends
    "ALTER TABLE messages ADD COLUMN group_id TEXT",
    # 0.2 -> 0.3: add caller reference for grouped sends
    "ALTER TABLE messages ADD COLUMN reference TEXT",
    # 0.3 -> 0.4: retry support
    "ALTER TABLE messages ADD COLUMN attempt_count INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN next_retry_at TEXT",
    "ALTER TABLE messages ADD COLUMN last_attempt_at TEXT",
]


@contextmanager
def get_connection():
    settings = get_settings()
    conn = sqlite3.connect(settings.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    from app.audit import AUDIT_SCHEMA
    with get_connection() as conn:
        conn.execute(SCHEMA)
        conn.execute(_API_KEYS_SCHEMA)
        conn.execute(_IDEMPOTENCY_SCHEMA)
        conn.execute(AUDIT_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        for stmt in _MIGRATIONS:
            match = re.search(r"ADD COLUMN (\S+)", stmt)
            col = match.group(1) if match else stmt.split()[-1]
            if col not in columns:
                conn.execute(stmt)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def create_message(
    message_id: str,
    channel: str,
    contact: str,
    message: str,
    status: str,
    group_id: Optional[str] = None,
    reference: Optional[str] = None,
) -> None:
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO messages
               (message_id, group_id, channel, contact, message, status, reference, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, group_id, channel, contact, message, status, reference, now, now),
        )


def update_status(
    message_id: str,
    status: str,
    provider: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE messages
               SET status = ?, provider = COALESCE(?, provider),
                   provider_message_id = COALESCE(?, provider_message_id),
                   error = ?, updated_at = ?
               WHERE message_id = ?""",
            (status, provider, provider_message_id, error, _now(), message_id),
        )


def get_message(message_id: str) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,))
        return cur.fetchone()


def get_group(group_id: str) -> Sequence[sqlite3.Row]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM messages WHERE group_id = ? ORDER BY created_at ASC",
            (group_id,),
        )
        return cur.fetchall()


def update_status_by_provider_id(
    provider_message_id: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE messages
               SET status = ?, error = ?, updated_at = ?
               WHERE provider_message_id = ?""",
            (status, error, _now(), provider_message_id),
        )


def list_messages(limit: int = 50, channel: Optional[str] = None) -> Sequence[sqlite3.Row]:
    with get_connection() as conn:
        if channel:
            cur = conn.execute(
                "SELECT * FROM messages WHERE channel = ? ORDER BY created_at DESC LIMIT ?",
                (channel, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return cur.fetchall()


def increment_attempt(message_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE messages
               SET attempt_count = attempt_count + 1,
                   last_attempt_at = ?,
                   updated_at = ?
               WHERE message_id = ?""",
            (_now(), _now(), message_id),
        )


def set_retry_schedule(message_id: str, next_retry_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE messages
               SET next_retry_at = ?, status = 'queued', updated_at = ?
               WHERE message_id = ?""",
            (next_retry_at, _now(), message_id),
        )


def reset_stale_processing(stale_timeout_minutes: int = 5) -> int:
    """Reset messages stuck in PROCESSING state back to QUEUED.

    Returns the number of messages reset.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_timeout_minutes)).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE messages
               SET status = 'queued', updated_at = ?
               WHERE status = 'processing' AND updated_at < ?""",
            (_now(), cutoff),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(
    key_hash: str,
    name: str,
    tenant_id: str,
    scopes: list[str],
    rate_limit_per_second: Optional[int] = None,
    expires_at: Optional[str] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO api_keys
               (key_hash, name, tenant_id, scopes, rate_limit_per_second, is_active, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (key_hash, name, tenant_id, json.dumps(scopes), rate_limit_per_second, _now(), expires_at),
        )


def get_api_key(key_hash: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (key_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["scopes"] = json.loads(result["scopes"]) if result["scopes"] else []
        return result


def revoke_api_key(key_hash: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?",
            (key_hash,),
        )
        return cur.rowcount > 0


def list_api_keys(limit: int = 50) -> list[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT key_hash, name, tenant_id, scopes, rate_limit_per_second, is_active, created_at, expires_at FROM api_keys ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        result = []
        for row in rows:
            r = dict(row)
            r["scopes"] = json.loads(r["scopes"]) if r["scopes"] else []
            result.append(r)
        return result


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def get_idempotency(key: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        # Parse cached response if present
        if result.get("response_body"):
            try:
                result["response_body"] = json.loads(result["response_body"])
            except (json.JSONDecodeError, TypeError):
                pass
        return result


def create_idempotency(
    key: str,
    message_id: str,
    status: str = "processing",
    response_body: Optional[Dict] = None,
    ttl_hours: int = 24,
) -> None:
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=ttl_hours)).isoformat()
    body_json = json.dumps(response_body) if response_body else None
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO idempotency_keys
               (idempotency_key, message_id, status, response_body, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, message_id, status, body_json, now.isoformat(), expires),
        )


def update_idempotency(
    key: str,
    status: str,
    response_body: Optional[Dict] = None,
) -> None:
    body_json = json.dumps(response_body) if response_body else None
    with get_connection() as conn:
        conn.execute(
            """UPDATE idempotency_keys
               SET status = ?, response_body = COALESCE(?, response_body)
               WHERE idempotency_key = ?""",
            (status, body_json, key),
        )


def cleanup_expired_idempotency() -> int:
    """Delete expired idempotency keys. Returns count deleted."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM idempotency_keys WHERE expires_at < ?",
            (_now(),),
        )
        return cur.rowcount
