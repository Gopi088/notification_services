"""
Lightweight SQLite persistence layer for tracking message/delivery state.

No ORM is used on purpose -- this service has one table and a handful of
queries, so raw sqlite3 keeps things simple and dependency-free.

A `group_id` links every channel attempt for one logical send, so a single
request fanning out to whatsapp+sms+email shares a group and can be queried
together via GET /api/v1/notifications/{group_id}/status.
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Sequence

from app.config import get_settings

_settings = get_settings()

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

_MIGRATIONS = [
    # 0.1 -> 0.2: add group_id for multi-channel sends
    "ALTER TABLE messages ADD COLUMN group_id TEXT",
    # 0.2 -> 0.3: add caller reference for grouped sends
    "ALTER TABLE messages ADD COLUMN reference TEXT",
]


@contextmanager
def get_connection():
    conn = sqlite3.connect(_settings.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        for stmt in _MIGRATIONS:
            match = re.search(r"ADD COLUMN (\S+)", stmt)
            col = match.group(1) if match else stmt.split()[-1]
            if col not in columns:
                conn.execute(stmt)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
