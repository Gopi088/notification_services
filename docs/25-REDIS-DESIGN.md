# 09 — Redis Design

## 9.1 Role

Redis is a **supporting** layer — it accelerates rate limiting, idempotency
checks, and temporary coordination. **PostgreSQL remains the durable source of
truth.** If Redis is lost, the system can rebuild its state from PostgreSQL and
continues to function (with rate limiting failing open).

## 9.2 Responsibilities (and explicit non-responsibilities)

| Used for | Not used for |
| -------- | ------------ |
| Rate limiting counters | Primary notification storage |
| Idempotency key fast-path cache | Long-term audit history |
| Distributed locks (only if truly needed, e.g. idempotent webhook fan-out) | User preferences as the system of record (DB if preferences are added) |
| Temporary processing locks / claim marker | Cross-DC authoritative state |

Rationale: rate limiting and idempotency need O(1) reads with TTLs — exactly what
Redis excels at. Durable state needs SQL semantics, joins, and guarantees — PostgreSQL.

## 9.3 Key Naming

All keys namespaced and TTL-bounded:

| Purpose | Key | TTL |
| ------- | --- | --- |
| Rate limit counter | `rl:{type}:{scope}:{value}` | bucket window |
| Idempotency cache | `idem:{key}` → `{notification_id}` | 24 h |
| Recently processed | `proc:{notification_id}` → `{status}` | 24 h |
| Distributed lock | `lock:{name}` | 10 s (with renewal) |
| Queue streams | `notifications:{channel}` / `notifications:retry` / `notifications:dlq` | — (streams, no TTL) |

## 9.4 Data Structures

| Use | Redis type |
| --- | ---------- |
| Rate limit counters | `INCR` + `EXPIRE`, or sorted set for sliding window log |
| Idempotency cache | `SET` string |
| Distributed lock | `SET key value NX PX <ttl>` |
| Queue | `STREAM` (XADD / XREADGROUP / XAUTOCLAIM) |
| Worker coordination | consumer group info |

## 9.5 Eviction & Persistence

- **Eviction:** `allkeys-lru` for cache/rate keys is acceptable, but rate-limit
  and idempotency keys have explicit TTLs so eviction is not relied upon. If a
  limit key is evicted early, the worst case is a slightly more lenient limit.
- **Persistence:** `appendonly yes`, `appendfsync everysec` so the queue
  streams survive a crash with at most ~1 s of loss. PostgreSQL reconciliation
  covers that loss.

## 9.6 Failure Behavior

| Failure | Impact | Recovery |
| ------- | ------ | -------- |
| Redis unavailable | Rate limiting fails open; idempotency fast-path skipped (falls back to PostgreSQL); queue consumers stop | Alert `redis.down`; when Redis returns, workers resume; orphaned `queued` rows re-enqueued by reconciliation |
| Idempotency cache miss | Falls back to PostgreSQL `idempotency_keys` — correct, just slower | — |
| Key expired early | Slightly more lenient limit; idempotency falls back to DB | — |
| Memory pressure | LRU evicts cache keys; streams unaffected (no TTL); watch memory | Add memory, or cluster |

## 9.7 Cache Miss Behavior (status/preferences)

- Notification status is always read from PostgreSQL (authoritative).
- Redis caches **hot** status lookups only if profiling shows a need
  (`cache:status:{id}` TTL 30 s, invalidated on status change). Not enabled by
  default to avoid stale reads.
- User/channel preferences, if added later, are cached in Redis with
  `pref:user:{id}` TTL 5 min and invalidated on write.

## 9.8 Why Redis (and not "everything")

Redis is used where it uniquely helps: sub-millisecond rate limiting and
idempotency checks, plus the message queue with consumer groups. It is **not**
used as a cache-of-record or as the notification database — those stay in
PostgreSQL.