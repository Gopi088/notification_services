"""
Lightweight SQLite persistence layer for tracking message/delivery state.

No ORM is used on purpose -- this service has one table and a handful of
queries, so raw sqlite3 keeps things simple and dependency-free.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings

_settings = get_settings()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    channel              TEXT NOT NULL,
    contact              TEXT NOT NULL,
    message               TEXT NOT NULL,
    status                TEXT NOT NULL,
    provider              TEXT,
    provider_message_id   TEXT,
    error                 TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
"""


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_message(message_id: str, channel: str, contact: str, message: str, status: str) -> None:
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO messages
               (message_id, channel, contact, message, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, channel, contact, message, status, now, now),
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
