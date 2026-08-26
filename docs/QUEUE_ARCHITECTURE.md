# Queue Architecture

## 3.1 Purpose

Defines how notifications are decoupled from provider delivery, with **two
supported queue backends**:

- `QUEUE_BACKEND=redis` — Redis Streams + worker processes (production /
  multi-container).
- `QUEUE_BACKEND=memory` — in-process asyncio queue for local / single-instance
  development.

The application must be configurable; Redis is not removed blindly.

## 3.2 Why a Queue

- The API must never block on a slow provider.
- Delivery must survive an API restart.
- Workers must scale independently of the API.
- A backlog must be absorbed without unbounded concurrency.

## 3.3 Redis Decision (current usage separated)

| Concern | Current mechanism | Redis role |
| ------- | ----------------- | ---------- |
| Queue | Redis Streams (`app/queue.py`) | transport (jobs) |
| Idempotency | Redis cache `idem:*` + PostgreSQL table | fast-path cache |
| Rate limiting | Redis counters `rl:*` | counters |
| Caching | (not used as primary cache) | — |

Redis is **not** the source of truth — PostgreSQL is. Redis is a transport +
transient state only.

## 3.4 Two Modes

### MODE A: `QUEUE_BACKEND=redis` (default for production)

- Producer: API `XADD notifications:<channel>`.
- Consumers: worker processes (`python3 -m app.worker_runner <channel>`) using
  Redis Streams consumer groups.
- Retry stream + dead-letter stream.
- Durable across API/worker restarts.

### MODE B: `QUEUE_BACKEND=memory` (default for local dev)

- In-process asyncio queue (no Redis required).
- Workers run as asyncio tasks inside the API process (or a dedicated process).
- Safe for single-instance development; **not** for multi-container HA.
- On process restart, pending in-memory jobs are lost, but **the notification
  is already persisted as `queued` in PostgreSQL**, and a reconciliation pass
  re-enqueues orphaned `queued` rows on startup.

## 3.5 Configuration

```env
# redis (production) | memory (local dev)
QUEUE_BACKEND=redis
QUEUE_ENABLED=true
REDIS_URL=redis://redis:6379/0
```

When `QUEUE_BACKEND=memory`, `QUEUE_ENABLED` may default to `true` with no
Redis dependency.

## 3.6 Job Payload

```json
{
  "event_id": "EVT_...",
  "notification_id": "internal-uuid",
  "group_id": "group-uuid",
  "channel": "whatsapp",
  "recipient": "+919887270348",
  "attempt": 1,
  "idempotency_key": "...",
  "request_id": "req_..."
}
```

The worker always reloads the full row from PostgreSQL by `notification_id`
(the queue is a transport, never the record of truth).

## 3.7 Request → Queue Flow

```
request
  → validate / idempotency / rate-limit
  → persist notification (status=queued)
  → enqueue job
  → return 202 Accepted
  → worker processes job
  → provider request
  → update status (submitted)
  → provider webhook
  → update final status (delivered / failed / read)
```

## 3.8 Queue Unavailable

If the queue cannot accept a job:

1. The notification is **already persisted** as `queued` (write happens before
   enqueue).
2. The API returns `503 queue_unavailable` (or `202` with a documented
   reconciliation guarantee when memory mode).
3. The failure is logged and audited (`queue_unavailable`).
4. A reconciliation job re-enqueues orphaned `queued` rows whose
   `next_attempt_at` has passed — nothing is silently lost.

## 3.9 Retry / DLQ

- Retryable failure → back to `retrying` + scheduled retry (exponential
  backoff, max delay).
- Retries exhausted → `dead_lettered` + DLQ stream entry + alert.
- Permanent failure (4xx, invalid recipient, auth) → `failed`, no retry.

## 3.10 Implementation

- Add `QUEUE_BACKEND` to `app/config.py`.
- Introduce a queue interface (`publish`, `consume`, `ack`) with two backends:
  - `app/queue.py` — Redis Streams (existing).
  - `app/memory_queue.py` — asyncio `asyncio.Queue` + in-process worker tasks.
- The orchestrator calls `publish()` via the configured backend.
- Startup reconciliation re-enqueues orphaned `queued` rows in both modes.
