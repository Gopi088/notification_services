"""
Durable AUDIT logging — completely separate from application logs.

Audit records answer: WHO? WHAT? WHEN? WHICH NOTIFICATION? WHICH CHANNEL?
WHAT RESULT? WHY FAILED?

Storage (both, for redundancy + durability across restart):
  1. PostgreSQL / SQLite `audit_logs` table (primary durable store).
  2. A dedicated audit file (JSON Lines) at AUDIT_LOG_FILE, separate from the
     application log file, so audit history survives even if the DB is down.

Application logs (temporary diagnostics) are handled by app/logging_config.py
and never replace the audit store.

Per docs/12-AUDIT-LOGGING.md.
"""
import json
import logging
import os
import threading
import uuid
from typing import Dict, Optional

from app.config import get_settings
from app.logging_config import mask
from app.storage import get_storage

logger = logging.getLogger("app.audit")

_AUDIT_LOCK = threading.Lock()

# Known audit actions (subset that actually occurs in the system).
AUDIT_EVENTS = {
    "notification_created",
    "notification_submitted",
    "notification_queued",
    "notification_scheduled",
    "notification_processing",
    "notification_sent",
    "notification_delivered",
    "notification_failed",
    "notification_retrying",
    "notification_cancelled",
    "duplicate_notification_attempted",
    "rate_limit_exceeded",
    "authorization_denied",
    "provider_failure",
    "notification_dead_lettered",
    "notification_deferred",
    "notification_resend_requested",
    "notification_resent",
    "authentication_success",
    "authentication_failed",
    "notification_expired",
    "retry_scheduled",
    "retry_attempted",
    "retry_exhausted",
    "queue_failure",
    "worker_failure",
    "idempotency_duplicate",
    "provider_webhook_received",
    "provider_webhook_rejected",
    "user_response_received",
    "notification_status_queried",
}


def _append_audit_file(record: Dict) -> None:
    """Append one JSON-line audit record to the dedicated audit file (if set)."""
    path = get_settings().AUDIT_LOG_FILE
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - never crash the flow on file failure
        logger.error("audit file append failed: %s", exc)


def record_audit(*, user_id: Optional[str], action: str,
                 notification_id: Optional[str] = None, channel: Optional[str] = None,
                 recipient: Optional[str] = None, status: Optional[str] = None,
                 provider: Optional[str] = None, ip_address: Optional[str] = None,
                 request_id: Optional[str] = None, result: str = "success",
                 failure_reason: Optional[str] = None,
                 metadata: Optional[Dict] = None,
                 source: Optional[str] = None) -> Optional[str]:
    """
    Persist one audit record to the DB table AND the dedicated audit file.

    `recipient` is stored MASKED (never full phone/email).
    Never pass secrets or full message bodies here.

    Every record includes:
      - timestamp, request_id (correlation), user_id, notification_id, channel
      - action, status, result, provider
      - error category/message (failure_reason)
      - database backend, source/service (in metadata)
    """
    from app.config import get_settings as _get_settings

    ref = mask(recipient) if recipient else None
    settings = _get_settings()
    db_backend = settings.STORAGE_BACKEND
    source = source or "notification_service"
    correlation_id = request_id or f"req_{uuid.uuid4().hex[:16]}"
    meta = dict(metadata or {})
    meta["database_backend"] = db_backend
    meta["source"] = source
    meta["correlation_id"] = correlation_id
    meta["error_category"] = failure_reason  # when applicable

    record: Dict = {
        "audit_id": f"AUD_{uuid.uuid4().hex[:12]}",
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "user_id": user_id or "anonymous",
        "action": action,
        "notification_id": notification_id,
        "channel": channel,
        "recipient_reference": ref,
        "status": status,
        "provider": provider,
        "ip_address": ip_address,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "database_backend": db_backend,
        "source": source,
        "result": result,
        "failure_reason": failure_reason,
        "metadata": meta,
    }

    # 1) Durable DB table.
    try:
        audit_id = get_storage().record_audit(
            user_id=record["user_id"], action=action, notification_id=notification_id,
            channel=channel, recipient_reference=ref, status=status,
            provider=provider, ip_address=ip_address, request_id=request_id,
            result=result, failure_reason=failure_reason, metadata=meta,
        )
        if audit_id:
            record["audit_id"] = audit_id
    except Exception as exc:  # noqa: BLE001 - audit must never crash the flow
        logger.error("audit DB record failed action=%s error=%s", action, exc)

    # 2) Dedicated audit file (independent of DB availability).
    _append_audit_file(record)

    # Informational app-log line (NOT the audit store).
    logger.info(
        "audit event recorded action=%s user_id=%s notification_id=%s "
        "channel=%s status=%s result=%s request_id=%s db_backend=%s source=%s",
        action, record["user_id"], notification_id, channel, status, result,
        request_id, db_backend, source,
    )
    return record["audit_id"]


def list_audit(limit: int = 50, user_id: Optional[str] = None,
               action: Optional[str] = None):
    """Return recent audit records from durable storage (DB table)."""
    return get_storage().list_audit(limit=limit, user_id=user_id, action=action)


def list_audit_from_file(limit: int = 50) -> list:
    """Read recent audit records from the dedicated audit file (if configured).

    Returns the most recent `limit` records (newest first).
    """
    path = get_settings().AUDIT_LOG_FILE
    if not path or not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.error("audit file read failed: %s", exc)
        return []
    return records[-limit:][::-1]
