# 04 — Message Queue

## 4.1 Technology Comparison

| Criteria | RabbitMQ | Kafka | Redis Streams |
| -------- | -------- | ----- | ------------- |
| Project complexity | Medium | High | Low |
| Throughput needs (this project) | Sufficient | Overkill | Sufficient |
| Ordering | Per-queue (limited) | Per-partition | Per-stream (FIFO per consumer group read) |
| Retries / DLQ | Native | Manual | Consumer groups + separate stream |
| Local dev / Docker | Extra Erlang runtime | Heavy (KRaft/Zookeeper) | Reuses existing Redis |
| Operational complexity | Medium | High | Low |
| Developer experience | Good | Steeper | Good |

**Recommendation: Redis Streams.**

Rationale:
- Redis is already required for rate limiting and idempotency caching (see
  [25-REDIS-DESIGN.md](25-REDIS-DESIGN.md)), so the queue adds **zero new
  infrastructure**.
- Consumer groups give at-least-once delivery, automatic redelivery of
  un-acked messages, and horizontal worker scaling.
- A dedicated dead-letter stream mirrors the notification state machine's
  `dead_lettered` status.
- Kafka's ordering/partitioning power is not needed for notification fan-out;
  its operational weight is unjustified here.
- RabbitMQ is a valid alternative if Redis Streams proves insufficient; the
  worker/queue abstraction (Section 4.9) keeps that swap contained.

## 4.2 Stream Topology

```
Stream: notifications:whatsapp      ← consumer group "workers"
Stream: notifications:sms           ← consumer group "workers"
Stream: notifications:email         ← consumer group "workers"
Stream: notifications:retry         ← delayed retries
Stream: notifications:dlq           ← dead letters
```

Per-channel streams allow per-channel concurrency control and rate limiting, and
prevent one channel's backlog from delaying others.

## 4.3 Message Format

```json
{
  "event_id": "EVT_100001",
  "notification_id": "MESSAGE-UUID",
  "group_id": "GROUP-UUID",
  "channel": "sms",
  "recipient": "+919887270348",
  "message": "Your interview is confirmed.",
  "attempt": 1,
  "idempotency_key": "idem-abc123",
  "request_id": "req_4f1a...",
  "created_at": "2026-08-24T17:30:00.123Z",
  "scheduled_at": null,
  "retryable": true
}
```

Only lightweight references and the minimal fields needed for a self-contained
job. The worker **always** loads the full row from PostgreSQL by
`notification_id` — the stream is not the source of truth.

## 4.4 Producer (API server)

1. Persist notification row (`status=queued`) **before** publishing (transaction).
2. `XADD notifications:<channel> * <json>`
3. Return 202.

Publish failure → notification stays `queued` in PostgreSQL; a reconciliation
job requeues orphaned `queued` rows older than N seconds.

## 4.5 Consumer (Worker)

1. `XREADGROUP GROUP workers <worker> COUNT 1 BLOCK 5000 STREAMS notifications:<channel> >`
2. Load notification from PostgreSQL.
3. Process (see [05-WORKER-DESIGN.md](05-WORKER-DESIGN.md)).
4. `XACK` on success or when the outcome is terminal/failed permanently.
5. On retryable failure: `XADD notifications:retry *` with `scheduled_at`; then `XACK`.

## 4.6 Acknowledgements

- **At-least-once**: a message is only removed from the stream's pending list on `XACK`.
- If the worker crashes before `XACK`, the message stays **pending** and is
  reassigned to another worker after the visibility timeout (see 4.7).
- Idempotency guarantees that a redelivered message does not double-send.

## 4.7 Visibility / Timeout

- Stream messages are visible to the group immediately.
- Pending messages (read but un-acked) are returned by `XAUTOCLAIM` after a
  worker-side processing timeout (default 30 s, configurable) and reassigned.
- The worker heartbeat updates `updated_at` so long-running provider calls do
  not cause premature reassignment.

## 4.8 Retry Behavior

- Retryable failure → message goes to `notifications:retry` with
  `scheduled_at = now + backoff` (see [07-RETRY-IDEMPOTENCY.md](07-RETRY-IDEMPOTENCY.md)).
- A dedicated retry consumer reads `notifications:retry`, and when
  `scheduled_at` is reached, moves it back to the channel stream.
- Non-retryable failure → message to `notifications:dlq` + DB status
  `dead_lettered`; alert fired.

## 4.9 Dead-Letter Queue

- Stream: `notifications:dlq`.
- Contains the original message + `dlq_reason`, `attempts`, `error_code`, `error_message`.
- Consumers of DLQ: alerting, manual inspection, optional re-queue via the
  retry endpoint.
- DLQ entries are themselves recorded in PostgreSQL `notification_attempts`
  (status `dead_lettered`) — the queue is a working set, not the record of truth.

## 4.10 Duplicate Messages

- Redis Streams does not deduplicate; **application-level idempotency** does.
- Worker checks the idempotency key (Redis fast path, PostgreSQL durable path)
  before sending.
- A redelivered message whose notification is already `submitted`/`delivered` is
  treated as a no-op (verify provider id matches, then ACK).

## 4.11 Ordering

- Within a single stream, messages are read in order by a single consumer, but
  consumer groups distribute across workers, so strict global ordering is **not**
  guaranteed. Notification ordering is best-effort.
- If per-recipient ordering matters (rare), the recipient hash can route to a
  partition key — out of scope for v1; documented as a future enhancement.

## 4.12 Durability

- Redis must run with AOF (append-only file) enabled and `appendfsync everysec`
  to minimize message loss on crash.
- Since PostgreSQL is the source of truth and `queued` rows are reconciled, a
  small queue loss window is acceptable and self-healing.

## 4.13 Backlog / Backpressure

- Queue depth metric (`XLEN`) and lag (`consumer group` lag) alert on thresholds.
- If a stream backlog exceeds a threshold, worker concurrency for that channel
  is throttled, and new API enqueues return `429` with `Retry-After` (see
  [08-RATE-LIMITING.md](08-RATE-LIMITING.md)).

## 4.14 Message Size and Stream Limits

- Redis stream entries are limited to ~512 MB each by Redis; the practical
  application limit is far lower. Enforce `QUEUE_MESSAGE_MAX_BYTES` (default
  64 KB) at publish — larger notifications are rejected (413/422) and never
  truncated.
- Streams are trimmed with `XADD ... MAXLEN ~ 100000` (approximate trimming) to
  bound memory. PostgreSQL is the durable record; trimming the stream only
  drops already-consumed/acked working-set entries.
- Message count per stream is bounded; a bounded stream prevents unbounded
  memory growth under sustained backlog.