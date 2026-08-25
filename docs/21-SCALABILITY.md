# 16 — Scalability

## 16.1 Scale Stages

| Stage | Users | Architecture |
| ----- | ----- | ------------ |
| S1 | 10–1,000 | Single API process, single worker, SQLite→PostgreSQL, Redis local. |
| S2 | 1,000–100,000 | API replicas behind LB, N workers on Redis Streams consumer group, managed PostgreSQL + Redis, rate limiting + idempotency enabled. |
| S3 | 100,000–1M+ | Horizontal autoscaling, Redis cluster, PostgreSQL read replicas + pooling, per-channel worker pools, batching, multi-AZ. |

## 16.2 Horizontal API Scaling

- API is stateless (auth, validation, enqueue) — trivially horizontally scalable.
- Scale trigger: CPU / request rate / P95 latency.
- Add replicas behind LB; readiness probe gates traffic.

## 16.3 Worker Scaling

- Workers are stateless consumers of the same Redis Streams consumer group.
- Scale trigger: queue lag / queue depth.
- Per-channel concurrency caps (provider rate limits) bound real egress, so
  adding workers never overruns a provider.

## 16.4 Queue Partitioning

- Streams are per-channel (`notifications:{channel}`), so channels scale
  independently and one backlog never blocks another.
- Within a channel, Redis Streams distributes across consumers.
- If a single stream becomes a bottleneck: partition by recipient-hash into
  N shard streams (`notifications:sms:0..N-1`) — deferred until needed.

## 16.5 Database

| Need | Technique | When |
| ---- | --------- | ----- |
| Correctness at scale | PostgreSQL (MVCC) | S1+ |
| Connection scalability | PgBouncer / pool (asyncpg) | S2 |
| Read scaling | read replicas for status/reporting reads | S2–S3 |
| Write scaling | keep writes minimal (one row + attempts); batch inserts where possible | S3 |
| Indexes | `(status, next_attempt_at)`, `(group_id)`, `(idempotency_key)`, `(provider_message_id)` | S1+ |

## 16.6 Redis

- Used for rate limiting, idempotency cache, queue streams.
- Scale: cluster mode when connections/memory grow; shard `rl:*` by key hash
  (natural sharding).
- Never the source of truth — its loss is self-healing from PostgreSQL.

## 16.7 Provider Rate Limits

- Providers cap real egress; the system must **never** exceed them.
- Enforcement: per-provider rate limit bucket (Redis) + per-channel worker
  concurrency + circuit breaker.
- This is the actual throughput ceiling — design to the provider quota, not raw CPU.

## 16.8 Backpressure & Batching

- Backpressure: 429 with `Retry-After` when queue backlog or rate limit hit.
- Batching: email is naturally batchable; SMS/WhatsApp are per-message. Batch
  only where the provider supports it (e.g., Azure email). Deferred unless a
  provider exposes a batch API.

## 16.9 Autoscaling

- API: HPA (Kubernetes) or managed LB auto-scaling by request rate.
- Workers: HPA by custom metric `queue_lag`.
- Not premature: start at S2 with manual scaling; add autoscaling when ops
  burden justifies it.

## 16.10 Do Not Prematurely Optimize

Optimization order:
1. Correct asynchronous architecture (S1/S2).
2. Rate limiting + idempotency (correctness at scale).
3. Replicas + managed services (S2).
4. Read replicas, pooling, sharding (S3).
Only add the next technique when the current one is measured to be the bottleneck.