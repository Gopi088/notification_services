# Reliability & Failures

## 4.1 Purpose

Defines retry policy, failure classification, idempotency, and behaviour under
provider / queue / database / Redis failures — and how to prevent duplicate
delivery.

## 4.2 Retryable vs Non-Retryable

### Retryable (transient)

- timeout
- connection failure
- temporary provider 5xx
- temporary rate limit (429) where the provider says retry later

### Non-Retryable (permanent — never retry)

- invalid destination / recipient
- authentication failure (401/403)
- insufficient provider balance
- invalid request / template error (400/422)

## 4.3 Backoff

- Exponential backoff with jitter: `delay = min(base * 2^(attempt-1), max) * (1±jitter)`.
- Default: `base=5s`, `max=120s`, `max_attempts=5`, `jitter=±20%`.
- Configured via `MAX_ATTEMPTS`, `RETRY_BASE_DELAY_MS`, `RETRY_MAX_DELAY_MS`,
  `RETRY_JITTER_RATIO`.

## 4.4 Duplicate Delivery Prevention

- **Idempotency keys** (client-provided or server-derived) deduplicate API
  requests.
- Worker re-checks the persisted status before sending; a redelivered job whose
  notification is already `submitted`/`delivered` is a no-op (ACK, no resend).
- Provider message IDs are stored so webhook events map to the correct
  notification exactly once.

## 4.5 Failure Handling by Layer

| Failure | Behaviour |
| ------- | --------- |
| Provider timeout | retryable → `retrying` + backoff |
| Provider 4xx | non-retryable → `failed` (reason stored) |
| Provider 5xx | retryable → `retrying` + backoff |
| Provider webhook delayed | status stays `submitted`; webhook updates when it arrives |
| Duplicate webhook | idempotent no-op (terminal state preserved) |
| Webhook for unknown message | recorded, audited, ignored |
| Queue unavailable | notification persisted; `503`; reconciliation re-enqueues |
| Database unavailable | API `503`; worker retries DB op |
| Redis unavailable | rate-limit/idempotency fail open; queue consumers retry |
| Memory queue restart | `queued` rows re-enqueued by reconciliation |
| Rate limit exceeded | `429`; logged + audited; not enqueued |
| Notification expires | `expired` (scheduled/out-of-hours window) |

## 4.6 Idempotency

- Idempotency key checked in Redis (fast) then PostgreSQL (durable).
- Conflicting payload with the same key → `409 idempotency_conflict`.
- Duplicate queue message → worker sees terminal status → no-op.
- Worker crash after provider accepted → redelivery detects `submitted` with
  matching provider id → no resend.

## 4.7 Reconciliation

On startup (and periodically), re-enqueue notifications that are `queued` or
`retrying` whose `next_attempt_at` has passed. This makes memory-queue restarts
and queue-outage windows self-healing.

## 4.8 Audit on Lifecycle

Every lifecycle transition writes an audit record (see
[NOTIFICATION_LIFECYCLE.md](NOTIFICATION_LIFECYCLE.md) and the audit section of
the test plan). Terminal failures include the failure category/message.

## 4.9 Implementation

- `app/retry.py` already implements backoff + classification.
- `app/worker.py` already routes retryable/non-retryable and dead-letter.
- Add reconciliation helper in the orchestrator / worker startup.
- Ensure webhook handling is idempotent (guard on terminal state).
