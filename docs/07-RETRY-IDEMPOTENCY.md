# 07 — Retry & Idempotency

## 7.1 Retry Policy

Default: **5 attempts total** (1 initial + 4 retries). This balances transient
provider outages against latency and provider costs.

```
Attempt 1  (initial)
   │ failure (retryable)
   ▼
backoff = 5s        Attempt 2
   │
   ▼
backoff = 10s       Attempt 3
   │
   ▼
backoff = 20s       Attempt 4
   │
   ▼
backoff = 40s       Attempt 5
   │ failure
   ▼
Dead Letter Queue (status = dead_lettered)
```

**Why 5 attempts:** most provider transient failures (5xx, throttling) resolve
within seconds to a minute. 5 attempts with backoff gives ~75 seconds of retry
horizon. More attempts increase double-delivery risk and cost with little benefit.

**Exponential backoff with jitter:**

```
delay_ms = min(base_delay × 2^(attempt-1), max_delay) + random(0, jitter_range)
base_delay = 5_000 ms
max_delay   = 120_000 ms
jitter      = ±20% of the computed delay
```

Jitter prevents a "retry storm" when a batch of messages fails together.

## 7.2 Retryable vs Non-retryable

| Category | Examples | Action |
| -------- | -------- | ------ |
| **Retryable** | network timeout, connection refused, HTTP 429, HTTP 5xx, provider temporary outage, DNS failure | backoff + retry |
| **Non-retryable** | invalid recipient, invalid credentials, invalid sender, unsupported channel, HTTP 400/401/403/404/422, permanent provider rejection, malformed request | fail immediately, no retry |

Classification lives in each provider (`is_retryable`). Workers never guess.

## 7.3 Retry Storage

- Retry schedule persisted in PostgreSQL: `retry_count`, `max_attempts`,
  `next_attempt_at`.
- The retry **trigger** is the `notifications:retry` stream (delayed delivery):
  a retry worker pops entries whose `scheduled_at` has passed and moves them back
  to the channel stream.
- `next_attempt_at` is the durable truth; the queue is the execution trigger.
  Reconciliation re-enqueues due-but-missing items.

## 7.4 Retry Storm Protection

- Jitter per above.
- Per-channel retry concurrency cap.
- Circuit breaker opens on repeated provider 5xx/429, shedding load.
- Global retry rate limit (Redis counter) prevents runaway requeue.

## 7.5 Idempotency

**Goal:** the same notification must not be sent twice due to duplicate API
requests, worker retries, queue redelivery, worker crashes, or timeout-after-accept.

**Idempotency key sources:**

1. Client `Idempotency-Key` header (recommended).
2. Fallback: deterministic key derived from `(channel, recipient, message_hash, reference)`
   computed server-side when the client supplies none.

**Storage:**

| Layer | Purpose |
| ----- | ------- |
| Redis | Fast path cache: `idem:{key}` → `{notification_id}` with TTL (default 24 h) |
| PostgreSQL | Durable dedup: `idempotency_keys` table (key PK, payload_hash, notification_id, expires_at) |

**Flow:**

```
Receive request with key K
  → Redis: GET idem:K ?
      yes → return existing notification result (no enqueue)
      no  → PostgreSQL: SELECT ... WHERE key = K ?
        yes → return existing
        no  → INSERT idempotency_keys(K, payload_hash, notification_id)  [unique]
             (conflict ⇒ concurrent duplicate → return existing)
      then enqueue + return 202
```

Conflicting payload with same key → `409 idempotency_conflict`.

**Worker side:**

```
Load notification
  → if status in (submitted, delivered) → no-op, ACK
  → else process
```

## 7.6 The Hard Case: Provider Accepted but Response Lost

Scenario: worker calls provider → provider sends → network times out before the
worker receives the response → worker retries → **duplicate send**.

Mitigation (as far as the provider allows):

1. **Timeout-then-verify:** on timeout, call `provider.get_status(provider_message_id)`
   if the provider exposes it; if the message exists, mark `submitted` and do **not** resend.
2. **Persist before send:** write `attempt` row with status `processing` and the
   provider request before invoking the provider; on redelivery, the worker sees
   an in-flight attempt and queries status instead of re-sending.
3. **Provider idempotency:** send a client-generated `client_ref`/external id
   that the provider dedupes against where supported (e.g., Vonage `client_ref`,
   Azure `repeatability` headers).
4. **Result:** full prevention of double-delivery is provider-dependent. The
   system guarantees **at-most-once-enqueue + at-most-once-acceptance-ack** and
   documents the residual window.

**Residual risk** is bounded and logged: a timeout-after-accept may deliver twice
for providers without idempotent send or status lookup. This is recorded in
`notification_attempts` and surfaced in metrics (`idem.uncertain_window`).

## 7.7 Queue Redelivery Safety

- Queue messages are safe to redeliver because the worker always re-reads the
  durable notification state from PostgreSQL and consults idempotency before
  sending. Redelivery of an already-`submitted`/`delivered` message is a no-op.