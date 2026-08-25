# 12 — Audit Logging

## 12.1 Purpose

Audit logs are a **durable business/security record**: *what happened, who did
it, when, which notification, which channel, what result, and why it failed*.

Audit is **not** terminal logging. Terminal logs are temporary operational
diagnostics; audit records persist in PostgreSQL and are tamper-evident.

## 12.2 Storage

Table `audit_logs` (see `03-DATA-MODEL.md`):

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | BIGSERIAL PK | monotonic |
| `audit_id` | TEXT | `AUD_<uuid>` |
| `timestamp` | TIMESTAMPTZ | event time (UTC) |
| `user_id` | TEXT | actor identity |
| `action` | TEXT | event name |
| `notification_id` | TEXT | nullable |
| `channel` | TEXT | nullable |
| `recipient_reference` | TEXT | masked recipient ref |
| `status` | TEXT | nullable |
| `provider` | TEXT | nullable |
| `ip_address` | TEXT | nullable |
| `request_id` | TEXT | correlation |
| `result` | TEXT | success / failure |
| `failure_reason` | TEXT | nullable |
| `metadata` | JSONB | extra redacted data |

Never store secrets, raw API keys, passwords, or full message bodies.

## 12.3 Audit Events

Recorded events (only those that occur):

```
notification_created
notification_submitted
notification_queued
notification_scheduled
notification_processing
notification_sent
notification_delivered
notification_failed
notification_retrying
notification_cancelled
duplicate_notification_attempted
rate_limit_exceeded
authorization_denied
provider_failure
notification_dead_lettered
```

## 12.4 Example Record

```json
{
  "audit_id": "AUD_10001",
  "timestamp": "2026-08-25T12:30:00Z",
  "user_id": "USR_1001",
  "action": "notification_created",
  "notification_id": "MSG_10001",
  "channel": "whatsapp",
  "status": "queued",
  "request_id": "REQ_10001",
  "result": "success"
}
```

## 12.5 Answers Provided

| Question | Field |
| -------- | ----- |
| WHO? | `user_id` |
| WHAT? | `action` |
| WHEN? | `timestamp` |
| WHICH NOTIFICATION? | `notification_id` |
| WHICH CHANNEL? | `channel` |
| WHAT RESULT? | `result`, `status` |
| WHY FAILED? | `failure_reason` |

## 12.6 Integrity

- Append-only writes (no UPDATE/DELETE in normal operation).
- Optional hash chaining (`prev_hash`/`row_hash`) for tamper evidence
  (see `26-AUTH-AUDIT-DESIGN.md`).
- `audit verify` command recomputes the chain.

## 12.7 Retention

- Config `AUDIT_RETENTION_DAYS` (default 365).
- Purge only when `AUDIT_PURGE_ENABLED=true`, and seal/export the chain head
  before deleting old rows.
- Indexes: `(user_id, timestamp)`, `(action, timestamp)`, `(notification_id)`.

## 12.8 Integration Points

- Orchestrator: records every state transition + duplicate detection.
- API: records auth failures, authorization denials, rate-limit hits.
- Worker: records provider submissions, failures, retries, DLQ.
- Webhook: records delivered/failed receipts.

## 12.9 Tests & Evals

- [`evals/audit.yaml`](evals/audit.yaml)
- [`27-TEST-PLAN.md`](27-TEST-PLAN.md)
- [`26-AUTH-AUDIT-DESIGN.md`](26-AUTH-AUDIT-DESIGN.md)
