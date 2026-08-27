"""
Notification orchestrator.

Single channel per request. Uses a thread-safe queue for background processing.

Flow:
1. API receives request → validates → creates DB record → puts in queue
2. Worker threads pick from queue → call provider → update DB status
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from app.audit import contact_hash, record as audit_record
from app.database import create_message, get_group, get_message
from app.middleware import request_id_var
from app.queue import QueueItem, message_queue
from app.schemas import Channel, SendRequest

logger = logging.getLogger("orchestrator")


def orchestrate_send(req: SendRequest, background_tasks) -> Dict:
    """
    Send a notification on ONE channel.

    Flow:
    1. Create DB record (status: queued)
    2. Put in queue for worker threads
    3. Return immediately

    Returns the message summary used for the 202 response.
    """
    ch = contact_hash(req.contact)
    logger.info(
        "Received send request: channel=%s contact_hash=%s reference=%s",
        req.channel.value, ch, req.reference,
        extra={"channel": req.channel.value, "message_id": None},
    )

    message_id = str(uuid.uuid4())

    logger.debug(
        "Generated message_id=%s for channel=%s",
        message_id, req.channel.value,
        extra={"channel": req.channel.value, "message_id": message_id},
    )

    # Create database record
    create_message(
        message_id=message_id,
        channel=req.channel.value,
        contact=req.contact,
        message=req.message,
        status="queued",
        group_id=None,
        reference=req.reference,
    )
    logger.info(
        "Created DB record: message_id=%s status=queued",
        message_id,
        extra={"message_id": message_id, "channel": req.channel.value},
    )

    # Build template params dict
    params = None
    if req.template_params:
        params = {p.name: p.value for p in req.template_params}

    # Put in queue for workers to pick up
    rid = request_id_var.get("")
    logger.debug(
        "Building queue item: message_id=%s request_id=%s template=%s",
        message_id, rid[:8] if rid else "none", req.template_name or "none",
        extra={"message_id": message_id, "channel": req.channel.value},
    )
    item = QueueItem(
        message_id=message_id,
        channel=req.channel.value,
        contact=req.contact,
        message=req.message,
        reference=req.reference,
        template_name=req.template_name,
        template_language=req.template_language,
        template_params=params,
        request_id=rid,
    )
    message_queue.put(item)
    logger.info(
        "Enqueued for worker processing: message_id=%s",
        message_id,
        extra={"message_id": message_id, "channel": req.channel.value},
    )

    # Audit record
    audit_record(
        action="notification.send",
        resource=message_id,
        outcome="success",
        detail={
            "channel": req.channel.value,
            "contact_hash": ch,
            "reference": req.reference,
        },
    )

    return {
        "message_id": message_id,
        "reference": req.reference,
        "channel": req.channel.value,
        "contact": req.contact,
        "status": "queued",
    }


def _delivery_detail(row) -> Dict:
    """Compute elapsed time and timeout flag for a message row."""
    from app.config import get_settings
    timeout = get_settings().DELIVERY_TIMEOUT_SECONDS
    detail = {
        "delivery_timeout_seconds": timeout,
        "elapsed_seconds": None,
        "timed_out": False,
    }
    created_raw = row["created_at"]
    if not created_raw:
        return detail
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return detail

    now = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = (now - created).total_seconds()
    detail["elapsed_seconds"] = round(elapsed, 1)

    if row["status"] in ("queued", "processing", "retrying", "sent") and elapsed > timeout:
        detail["timed_out"] = True
    return detail


def get_message_summary(message_id: str) -> Optional[Dict]:
    """Look up a single message and return a public-friendly dict."""
    row = get_message(message_id)
    if row is None:
        return None

    detail = _delivery_detail(row)
    return {
        "message_id": row["message_id"],
        "channel": row["channel"],
        "contact": row["contact"],
        "message": row["message"],
        "status": row["status"],
        "provider": row["provider"],
        "provider_message_id": row["provider_message_id"],
        "error": row["error"],
        "reference": row["reference"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "attempt_count": row["attempt_count"] if "attempt_count" in row.keys() else 0,
        **detail,
    }


def get_group_summary(group_id: str) -> Optional[Dict]:
    """Aggregate per-channel statuses for one group into a public summary."""
    rows = get_group(group_id)
    if not rows:
        return None

    channels = []
    reference = None
    for row in rows:
        reference = row["reference"] if row["reference"] else reference
        detail = _delivery_detail(row)
        channels.append({
            "message_id": row["message_id"],
            "channel": row["channel"],
            "contact": row["contact"],
            "status": row["status"],
            "provider": row["provider"],
            "provider_message_id": row["provider_message_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "attempt_count": row["attempt_count"] if "attempt_count" in row.keys() else 0,
            **detail,
        })

    statuses = [ch["status"] for ch in channels]
    if all(s == "delivered" for s in statuses):
        overall = "delivered"
    elif all(s == "failed" for s in statuses):
        overall = "failed"
    elif any(s in ("queued", "processing", "retrying", "sent") for s in statuses):
        overall = "pending"
    else:
        overall = statuses[0] if statuses else "unknown"

    return {
        "message_id": group_id,
        "reference": reference,
        "status": overall,
        "channels": channels,
    }
