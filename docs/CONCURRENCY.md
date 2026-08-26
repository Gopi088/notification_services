# Concurrency

## 5.1 Goal

If 100 users send notifications simultaneously, the API/server must not fail
because providers are slow. Every valid request is persisted and placed into a
queue; workers process queued notifications safely.

## 5.2 Required Flow

```
Multiple clients
    ↓
POST /api/v1/notifications/send
    ↓
Authentication
    ↓
Validation
    ↓
Rate limiting
    ↓
Idempotency
    ↓
Persist notification as QUEUED
    ↓
Publish job to queue
    ↓
Return HTTP 202 immediately
    ↓
Worker consumes queued jobs
    ↓
Provider sends SMS/WhatsApp/Email
    ↓
Update status
    ↓
Retry transient failures
```

## 5.3 Rules

1. **Never** call the SMS/WhatsApp/Email provider synchronously inside the API
   request.
2. Every accepted notification is first persisted with `status=queued`.
3. The API returns `202 Accepted` with `notification_id`, `group_id`,
   `request_id`, and `status=queued`.
4. Multiple simultaneous requests are handled safely (tested with 10, 50, 100).
5. The queue provides backpressure: if providers are slow, notifications remain
   queued instead of crashing the API.
6. Workers consume notifications independently from the API.
7. Multiple workers can process queued notifications concurrently.
8. We do **not** create one thread per user/request.
9. We do **not** use unbounded memory queues in production.
10. Redis queue is kept for production; an optional in-memory queue exists only
    for local development/testing.

## 5.4 Queue Configuration

```env
QUEUE_ENABLED=true
QUEUE_BACKEND=redis      # redis (production) | memory (local dev)
REDIS_URL=redis://redis:6379/0
WORKER_CONCURRENCY=4
WORKER_CONCURRENCY_WHATSAPP=2
WORKER_CONCURRENCY_SMS=4
WORKER_CONCURRENCY_EMAIL=4
```

- `QUEUE_BACKEND=redis` → Redis Streams + worker processes (production).
- `QUEUE_BACKEND=memory` → in-process asyncio queue (single-instance dev only).

## 5.5 Provider Concurrency / Rate Limits

- Per-channel worker concurrency caps provider load
  (`WORKER_CONCURRENCY_WHATSAPP=2`, etc.).
- Per-provider rate limit buckets in Redis (`rl:provider:*`).
- Circuit breaker sheds load when a provider repeatedly fails.
- Providers are never overloaded beyond configured limits.

## 5.6 Database Transaction Safety

- The notification is persisted (`status=queued`) **before** queue publishing.
- If queue publishing fails, the row still exists; a reconciliation job
  re-enqueues orphaned `queued` rows whose `next_attempt_at` has passed.
- The durable idempotency key claim (DB unique constraint) is the concurrency
  mutex — only one of N concurrent identical requests creates a notification.

## 5.7 Worker Crashes

- Queued/processing jobs are recoverable: pending Redis Streams messages are
  reclaimed via `XAUTOCLAIM`; orphaned `queued` rows are re-enqueued by
  reconciliation.
- Duplicate delivery is prevented by idempotency keys and provider message IDs
  (a redelivered job whose notification is already `submitted`/`delivered` is a
  no-op).

## 5.8 Concurrency vs Threads vs Processes

| Concept | Meaning | Used for |
| ------- | ------- | -------- |
| Async request handling | Single event loop handles many requests without blocking | API (FastAPI async endpoints) |
| Background worker | A process/task that consumes queued jobs | Delivery (worker processes / asyncio tasks) |
| Thread | One execution flow sharing memory | Bounded worker threads (per-channel concurrency) |
| Process | Isolated execution with own memory | Multiple API/worker containers/processes |
| Queue | Buffer decoupling producers from consumers | Job handoff (Redis Streams / memory queue) |

## 5.9 Status Lifecycle

```
POST → 202 queued
immediate GET → queued
worker starts → processing
provider accepts → submitted
provider webhook → delivered
```

The status endpoint returns the persisted state immediately and never waits for
the provider. See [STATUS_LIFECYCLE.md](STATUS_LIFECYCLE.md).

## 5.10 Concurrency Test Results

See `tests/test_concurrent.py`:

- `test_10_concurrent` — 10 simultaneous requests, all accepted, all persisted.
- `test_50_concurrent` — 50 simultaneous requests, all accepted, no duplicates.
- `test_100_concurrent_no_crash` — 100 simultaneous requests, API does not
  crash, no data loss, no duplicates (SQLite may reject ~1 due to
  single-writer lock contention; production PostgreSQL handles all 100).
- `test_concurrent_idempotency_same_key` — 100 concurrent requests with the
  same Idempotency-Key → exactly 1 notification created (DB unique constraint
  as mutex).
