# 10 — Multi-User Concurrency

## 10.1 Requirement

Multiple users send notifications concurrently. Requests must not be processed
sequentially in a single blocking API process. The system is designed for
concurrent requests end-to-end:

```
User A ─┐
User B ─┤
User C ─┼──> API ──> Queue ──> Workers
User D ─┤
User E ─┘
```

## 10.2 Concurrency Model

| Layer | Concurrency mechanism |
| ----- | --------------------- |
| API | Async FastAPI handlers; `uvicorn` handles concurrent requests per process; horizontal replicas behind LB. |
| Database | Connection pooling (`ThreadedConnectionPool` for SQLite, psycopg2 pool / asyncpg pool for PostgreSQL). |
| Queue | Redis Streams consumer group distributes messages across workers. |
| Worker | Configurable per-channel concurrency (`WORKER_CONCURRENCY_*`), bounded by semaphores. |
| Provider | Calls are synchronous per worker but concurrent across workers; rate-limited via Redis. |
| Redis | Single shared instance; rate-limit/idempotency ops are O(1). |

## 10.3 API Concurrency

- FastAPI async endpoints never block on the provider (the API only validates,
  persists, and enqueues).
- Each request gets a unique `request_id`.
- The storage layer uses connection pooling so many requests share DB
  connections safely.
- Idempotency key writes use a unique constraint to resolve races (first
  writer wins).

## 10.4 Worker Concurrency

- Each worker process runs a bounded number of threads per channel.
- Concurrency values (config): `WORKER_CONCURRENCY` (default 4),
  `WORKER_CONCURRENCY_WHATSAPP=2`, `_SMS=4`, `_EMAIL=4`.
- Workers share the Redis Streams consumer group; Redis distributes messages.

## 10.5 Queue Concurrency

- One stream per channel (`notifications:{channel}`) + retry + DLQ streams.
- Consumer group `workers`; multiple workers consume independently.
- Pending-message reclaim (`XAUTOCLAIM`) handles crashed workers.

## 10.6 Database Concurrency

- Connection pool with configurable min/max (`DB_POOL_MIN/MAX`).
- Optimistic status transitions (`UPDATE ... WHERE status IN (...)`) prevent
  double-processing.
- Unique constraints on `message_id` and `idempotency_keys.key`.

## 10.7 Provider Concurrency

- Per-channel worker concurrency caps provider load.
- Provider rate-limit buckets in Redis (`rl:provider:*`).
- Circuit breaker opens after repeated failures to shed load.

## 10.8 Backpressure

- Queue depth (`XLEN`) and lag metrics alert on thresholds.
- When backlog exceeds a threshold, new API enqueues return `429` with
  `Retry-After`.
- Worker concurrency is never unbounded.

## 10.9 Consistency Under Concurrency

- Ownership: every notification has `created_by` (user id). Status lookups are
  scoped by user.
- No double-send: optimistic status guard + idempotency key + Redis replay
  check.
- Audit records are append-only; no shared mutable counters.

## 10.10 Scale Targets

| Concurrent requests | Behavior |
| ------------------- | -------- |
| 1 | normal path |
| 10 | all accepted, queued, processed |
| 100 | all accepted, queued, processed within limits |
| 1,000 | accepted (rate-limited above per-key/per-recipient caps), queue absorbs backlog, workers drain at provider rate |

## 10.11 Tests & Evals

Concurrency tests and evals:

- [`evals/concurrency.yaml`](evals/concurrency.yaml)
- [`evals/multi_user.yaml`](evals/multi_user.yaml)
- [`27-TEST-PLAN.md`](27-TEST-PLAN.md)
