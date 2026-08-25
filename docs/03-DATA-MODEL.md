# 03 — Data Model (PostgreSQL)

PostgreSQL is the **durable source of truth**. All tables below are needed for the
asynchronous, retryable, idempotent design. SQLite (current) maps 1:1 to this model.

## 3.1 Table: `notifications`

One row per logical notification message (one channel = one notification; a
multi-channel request fans out to N rows sharing a `group_id`).

| Column | Type | Null | Notes |
| ------ | ---- | ---- | ----- |
| `id` | UUID PK | no | internal id |
| `message_id` | UUID | no | public/external id returned to caller (unique) |
| `group_id` | UUID | yes | shared across channels of one request |
| `external_message_id` | VARCHAR(128) | yes | caller-supplied message id (e.g. `MSG_100001`) |
| `channel` | VARCHAR(32) | no | `whatsapp`, `sms`, `email` |
| `recipient` | VARCHAR(254) | no | phone or email |
| `message` | TEXT | no | body content |
| `subject` | VARCHAR(200) | yes | email subject |
| `template_name` | VARCHAR(128) | yes | external template ref |
| `template_language` | VARCHAR(32) | yes | |
| `template_params` | JSONB | yes | rendered params |
| `status` | VARCHAR(32) | no | state machine (3.6) |
| `provider` | VARCHAR(64) | yes | selected provider name |
| `provider_message_id` | VARCHAR(128) | yes | external provider id |
| `retry_count` | INTEGER | no | default 0 |
| `max_attempts` | INTEGER | no | default 5 |
| `next_attempt_at` | TIMESTAMPTZ | yes | when to retry |
| `idempotency_key` | VARCHAR(128) | yes | client key |
| `request_id` | VARCHAR(64) | yes | correlation |
| `created_by` | VARCHAR(64) | yes | api key id |
| `reference` | VARCHAR(128) | yes | caller ref |
| `last_error` | TEXT | yes | last failure reason |
| `dead_lettered_at` | TIMESTAMPTZ | yes | |
| `created_at` | TIMESTAMPTZ | no | |
| `updated_at` | TIMESTAMPTZ | no | |
| `scheduled_at` | TIMESTAMPTZ | yes | future scheduling |

**Indexes**

- `(status, next_attempt_at)` — worker scan of due items.
- `(group_id)` — status lookup by group.
- `(idempotency_key)` UNIQUE partial (WHERE idempotency_key IS NOT NULL).
- `(provider_message_id)` — webhook lookup.
- `(channel, created_at)` — reporting.

## 3.2 Table: `notification_attempts`

Append-only history of every send attempt.

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | BIGSERIAL PK | |
| `notification_id` | UUID FK → notifications.id | |
| `attempt` | INTEGER | 1-based |
| `provider` | VARCHAR(64) | |
| `status` | VARCHAR(32) | `submitted`, `failed` |
| `provider_message_id` | VARCHAR(128) | if provider acked |
| `error_code` | VARCHAR(64) | provider error code |
| `error_message` | TEXT | |
| `retryable` | BOOLEAN | classification |
| `duration_ms` | INTEGER | provider latency |
| `created_at` | TIMESTAMPTZ | |

**Index:** `(notification_id, attempt)`; `(provider_message_id)`.

## 3.3 Table: `notification_events`

Status change timeline (also feeds audit).

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | BIGSERIAL PK | |
| `notification_id` | UUID FK | |
| `from_status` | VARCHAR(32) | |
| `to_status` | VARCHAR(32) | |
| `actor` | VARCHAR(64) | api key / worker / webhook |
| `detail` | JSONB | extra |
| `created_at` | TIMESTAMPTZ | |

## 3.4 Table: `idempotency_keys`

Durable dedup backing store (Redis is the fast path cache).

| Column | Type | Notes |
| ------ | ---- | ----- |
| `key` | VARCHAR(128) PK | hashed idempotency key |
| `notification_id` | UUID | original result |
| `payload_hash` | CHAR(64) | sha256 of request to detect conflicts |
| `created_at` | TIMESTAMPTZ | |
| `expires_at` | TIMESTAMPTZ | TTL |

## 3.5 Table: `webhook_events`

Raw provider delivery receipts (for reconciliation and audit).

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | BIGSERIAL PK | |
| `provider` | VARCHAR(64) | |
| `provider_message_id` | VARCHAR(128) | |
| `status` | VARCHAR(32) | `delivered` / `failed` / `read` |
| `error_code` | VARCHAR(64) | |
| `error_message` | TEXT | |
| `payload` | JSONB | redacted raw event |
| `received_at` | TIMESTAMPTZ | |

**Index:** `(provider_message_id)`.

## 3.6 Notification State Machine

```
                  ┌────────────┐
                  │   queued   │
                  └─────┬──────┘
                        │ worker picks up
                        ▼
                  ┌──────────────┐
        ┌────────▶│  processing  │◀────┐ retry (attempts < max)
        │         └──────┬───────┘     │
        │                │             │
        │                ▼             │
        │         ┌─────────────┐      │
        │   ┌────▶│  submitted   │──────┘  provider acked; waiting for webhook
        │   │     └─────────────┘
        │   │  provider rejects / timeout (retryable)
        │   │
        │   ▼
        │  ┌────────┐     exhausted    ┌────────────────┐
        │  │failed  │───────────────▶ │  dead_lettered  │
        │  └────────┘                  └────────────────┘
        │
        └──────────── (webhook) ──▶ delivered
```

| Transition | Trigger | Notes |
| ---------- | ------- | ----- |
| `queued → processing` | worker claims message | |
| `processing → submitted` | provider accepted (has message_id) | wait for webhook |
| `processing → retrying` | retryable error, attempts < max | scheduled `next_attempt_at` |
| `retrying → processing` | retry time reached | |
| `submitted → delivered` | webhook `delivered` | terminal |
| `submitted → failed` | webhook `failed` / timeout | |
| `processing → failed` | non-retryable error | terminal |
| `failed → retrying` | manual retry or policy | |
| `processing → dead_lettered` | attempts exhausted | |
| `cancelled` | manual cancel | optional |

**Provider capability mapping:** SMS/WhatsApp/Email providers return `submitted` on
acceptance; true `delivered` only arrives via webhook receipts (or is best-effort in
MOCK_MODE).

## 3.7 Retention

- `notification_attempts` and `notification_events`: retain N days (default 90) then archive.
- `notifications`: retain indefinitely or per compliance policy (default 365 days for active, then cold storage).
- `webhook_events`: retain 30 days for reconciliation.
- Retention implemented as scheduled batch delete, never in the request path.