# 05 — Worker Design

## 5.1 Worker Role

A worker is a standalone process (or thread pool within one process) that consumes
from a Redis Stream consumer group and delivers notifications through the provider
layer. Workers are horizontally scalable and share the same consumer group.

## 5.2 Worker Lifecycle

```
1. Load config (env), connect to PostgreSQL + Redis
2. Register consumer group (create if missing: XGROUP CREATE ... MKSTREAM)
3. Blocking read: XREADGROUP GROUP workers <worker-id> COUNT n BLOCK 5000
4. For each message:
   a. Load notification from PostgreSQL
   b. Idempotency check
   c. Update status → processing
   d. Select provider (factory)
   e. Send
   f. Update status / attempts / events
   g. XACK or route to retry/DLQ
5. Graceful shutdown (SIGTERM): stop reading, finish in-flight, XAUTOCLAIM not needed
```

## 5.3 Message Flow

```
Worker
  ↓ XREADGROUP (blocking)
Consume queue message
  ↓
Load notification from PostgreSQL (by notification_id)
  ↓
Check idempotency (Redis then PostgreSQL)
  ↓
Update status → processing (optimistic guard)
  ↓
Select provider (factory, per channel)
  ↓
Send via provider
  ↓
Update status → submitted (provider_message_id) | failed (reason)
  ↓
Record notification_attempts + notification_events
  ↓
Retryable? → route to retry stream   |   XACK
Dead-letter? → route to DLQ stream   |   XACK
```

## 5.4 Concurrency

- Each worker runs N concurrent goroutines/tasks (configurable, default 4-8).
- Concurrency is **per channel** to respect provider rate limits:
  - `WORKER_CONCURRENCY_WHATSAPP=2`, `WORKER_CONCURRENCY_SMS=4`, `WORKER_CONCURRENCY_EMAIL=4`.
- Use a worker-local semaphore per channel. Provider rate limits are enforced
  additionally in Redis (see [08-RATE-LIMITING.md](08-RATE-LIMITING.md)).

## 5.5 Acknowledgement

- **Success / terminal failure**: `XACK` immediately.
- **Retryable failure**: `XADD notifications:retry` with backoff, then `XACK`
  the original (the retry stream owns the next attempt). This keeps the main
  stream clean and avoids infinite re-delivery loops.
- **Crash mid-send**: no `XACK`; message stays pending; another worker reclaims
  after `XAUTOCLAIM` timeout (default 30 s). Idempotency prevents double-send.

## 5.6 Crash / Failure Behaviors

| Scenario | Behavior |
| -------- | -------- |
| Provider unavailable | Classify retryable, backoff, retry stream. Never crash. |
| Database unavailable | Worker retries the DB operation with backoff; does not ACK until persisted. Alert after N failures. |
| Queue unavailable | Worker blocks and retries the read; logs and alerts; no data loss (messages remain in PostgreSQL). |
| Worker crashes after provider accepted | Provider id persisted only after response; on redelivery, idempotency check finds `submitted` with same provider id → no-op ACK. |
| Worker crashes before ACK | Message reclaimed and reprocessed; idempotency prevents double-send. |
| Duplicate message arrives | Idempotency key lookup returns original → ACK without re-sending. |
| Poison message (malformed) | Worker validates schema; malformed → DLQ with `dlq_reason=malformed`, alert. |

## 5.7 Graceful Shutdown

- `SIGTERM`/`SIGINT` → stop acquiring new messages.
- Finish in-flight sends (wait up to `WORKER_GRACE_SECONDS`, default 30 s).
- Persist statuses, ACK in-flight, close connections.
- If grace expires, exit; pending messages are reclaimed by the group.

## 5.8 Worker Scaling

- Workers are stateless and share the consumer group; scaling = add/remove
  processes/containers.
- Redis Streams distributes pending messages across active consumers via the
  `>` (new messages) and `XAUTOCLAIM` (pending) reads.
- Autoscaling signal: consumer-group lag and queue length.

## 5.9 Idempotency Guard on Status Update

To avoid two workers processing the same notification concurrently (e.g., due to
reclaim race), the `processing` transition uses an **optimistic update**:

```sql
UPDATE notifications
SET status='processing', updated_at=now()
WHERE id=$1 AND status IN ('queued','retrying')
```

Zero rows affected ⇒ another worker already owns it ⇒ ACK and skip.

## 5.10 Provider Selection

The worker calls the same `get_provider(channel)` factory as the API (shared
module). The provider interface is the only touchpoint for external APIs
([06-NOTIFICATION-PROVIDERS.md](06-NOTIFICATION-PROVIDERS.md)).