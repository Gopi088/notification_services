"""
Audit trail.

Persistent log of security-relevant actions (sends, auth failures, webhook
updates). Each record is written to the audit_log table and also emitted
as a structured log line.

Usage:
    from app.audit import record
    record("notification.send", resource=group_id, outcome="success",
           detail={"channels": 3, "reference": ref})
"""
import hashlib
import json
import logging
from typing import Any, Dict, Optional

from app.middleware import request_id_var

logger = logging.getLogger("audit")

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    request_id   TEXT,
    actor_key_id TEXT,
    action       TEXT NOT NULL,
    resource     TEXT,
    outcome      TEXT NOT NULL,
    detail       TEXT
);
"""

_SECRET_KEYS = frozenset({
    "authorization", "accesskey", "access_key", "secret", "password",
    "token", "connection_string", "connectionstring", "signingkey",
    "api_key", "api_secret", "vonage_api_key", "vonage_api_secret",
})


def _redact_dict(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: "***" if k.lower() in _SECRET_KEYS else _redact_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_redact_dict(v) for v in d]
    return d


def _redact(v: Any) -> Any:
    return _redact_dict(v)


def contact_hash(contact: str) -> str:
    return hashlib.sha256(contact.encode()).hexdigest()[:12]


def record(
    action: str,
    resource: Optional[str] = None,
    outcome: str = "success",
    detail: Optional[Dict[str, Any]] = None,
    actor_key_id: Optional[str] = None,
) -> None:
    from app.database import get_connection
    from app.middleware import request_id_var

    rid = request_id_var.get("")
    ts = _now()

    safe_detail = _redact(detail) if detail else None
    detail_json = json.dumps(safe_detail) if safe_detail else None

    log_msg = f"action={action} outcome={outcome}"
    if resource:
        log_msg += f" resource={resource}"
    if rid:
        log_msg += f" request_id={rid}"
    logger.info(log_msg)

    try:
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO audit_log (ts, request_id, actor_key_id, action, resource, outcome, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, rid or None, actor_key_id, action, resource, outcome, detail_json),
            )
    except Exception:
        logger.exception("Failed to write audit record")


def list_audit(
    limit: int = 50,
    action_filter: Optional[str] = None,
) -> list:
    from app.database import get_connection

    with get_connection() as conn:
        if action_filter:
            cur = conn.execute(
                "SELECT * FROM audit_log WHERE action LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{action_filter}%", limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
