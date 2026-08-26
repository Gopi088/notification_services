# Retry

## 6.1 Purpose

Defines retry policy: which failures are retryable, backoff, maximum attempts,
and how to prevent duplicate delivery during retries.

## 6.2 Retryable vs Non-Retryable

### Retryable (transient) — retry with backoff

- timeout
- connection failure
- temporary provider 5xx
- temporary rate limit (429) where the provider says retry later
- temporary provider outage

### Non-Retryable (permanent) — never retry

- invalid destination / recipient
- invalid credentials (401/403)
- insufficient provider balance
- invalid request / template error (400/422)
- permanent provider rejection

## 6.3 Backoff

Exponential backoff with jitter:

```
delay_ms = min(base * 2^(attempt-1), max) * (1 ± jitter)
```

Defaults:

- `MAX_ATTEMPTS=5`
- `RETRY_BASE_DELAY_MS=5000`
- `RETRY_MAX_DELAY_MS=120000`
- `RETRY_JITTER_RATIO=0.2`

## 6.4 Retry Flow

```
Attempt 1 (provider fails transient)
  → retrying + backoff (5s ± jitter)
Attempt 2
  → backoff (10s ± jitter)
Attempt 3
  → backoff (20s ± jitter)
...
Attempt N (max reached)
  → dead_lettered (DLQ) + alert
```

- Retries are scheduled via the retry stream and persisted
  (`retry_count`, `next_attempt_at`).
- A retry worker moves due retries back to the channel stream.

## 6.5 Duplicate Delivery Prevention

- Idempotency keys deduplicate API requests (DB unique constraint as mutex).
- Worker re-checks persisted status before sending; a redelivered job whose
  notification is already `submitted`/`delivered` is a no-op (ACK, no resend).
- Provider message IDs map webhook events to the correct notification exactly
  once.
- Worker crash after provider accepted → redelivery detects `submitted` with
  matching provider id → no resend.

## 6.6 Audit Events

Every retry transition is audited:

- `retry_scheduled`
- `retry_attempted`
- `retry_exhausted`
- `notification_retrying`
- `notification_failed`

See [NOTIFICATION_LIFECYCLE.md](NOTIFICATION_LIFECYCLE.md) and
[STATUS_LIFECYCLE.md](STATUS_LIFECYCLE.md).

## 6.7 Implementation

- `app/retry.py` — backoff + classification.
- `app/worker.py` — routes retryable/non-retryable; dead-letters on exhaustion.
- `app/queue.py` — retry + DLQ streams.
