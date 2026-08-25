# Notification System Documentation

Entry point for all architecture and specification documents for the
Notification Service project.

## Architecture

```
Client
  ↓  HTTP + API key
API Server (FastAPI, horizontally scalable)
  ↓  validate + enqueue
PostgreSQL ── durable source of truth (notifications, attempts, events)
  ↓
Message Queue (Redis Streams)
  ↓
Workers / Consumers (horizontally scalable)
  ↓
Provider Layer (abstraction)
  ├── SMS       (Vonage / Azure)
  ├── WhatsApp  (Vonage Sandbox / Azure)
  └── Email     (Azure)
  ↓
External Providers  ──delivery receipts──▶ webhook → DB/audit

Redis (supporting, never the source of truth)
  ├── rate limiting
  ├── idempotency keys
  └── caching / coordination
```

## Document Index

| # | Document | Purpose |
| - | -------- | ------- |
| 01 | [01-ARCHITECTURE.md](01-ARCHITECTURE.md) | Current + target architecture, component responsibilities, flows |
| 02 | [02-API-SPECIFICATION.md](02-API-SPECIFICATION.md) | Complete API contract: endpoints, payloads, status codes, error format |
| 03 | [03-DATA-MODEL.md](03-DATA-MODEL.md) | PostgreSQL schema, notification state machine, indexes, retention |
| 04 | [04-MESSAGE-QUEUE.md](04-MESSAGE-QUEUE.md) | Queue technology selection (Redis Streams), message format, DLQ |
| 05 | [05-WORKER-DESIGN.md](05-WORKER-DESIGN.md) | Worker/consumer lifecycle, ack semantics, crash behavior, scaling |
| 06 | [06-NOTIFICATION-PROVIDERS.md](06-NOTIFICATION-PROVIDERS.md) | Provider abstraction, per-channel docs, error mapping, retryability |
| 07 | [07-RETRY-IDEMPOTENCY.md](07-RETRY-IDEMPOTENCY.md) | Retry policy, exponential backoff + jitter, idempotency strategy |
| 08 | [08-RATE-LIMITING.md](08-RATE-LIMITING.md) | Rate limiting algorithms, keys, TTLs, burst behavior, per-channel limits |
| 09 | [09-SCHEDULING-QUIET-HOURS.md](09-SCHEDULING-QUIET-HOURS.md) | Scheduled notifications, timezone handling, allowed send windows, quiet hours |
| 10 | [10-MULTI-USER-CONCURRENCY.md](10-MULTI-USER-CONCURRENCY.md) | Multi-user concurrency, backpressure, scaling, consistency |
| 11 | [11-AUTHORIZATION.md](11-AUTHORIZATION.md) | Authorization model, roles, permissions, enforcement |
| 12 | [12-AUDIT-LOGGING.md](12-AUDIT-LOGGING.md) | Durable audit log: events, schema, retention, integrity |
| 13 | [13-APPLICATION-LOGGING.md](13-APPLICATION-LOGGING.md) | Operational logging: levels, events, correlation, masking |
| 14 | [14-OBSERVABILITY.md](14-OBSERVABILITY.md) | Logging, metrics, health/readiness/liveness |
| 15 | [15-TESTING-STRATEGY.md](15-TESTING-STRATEGY.md) | 90% coverage strategy, test layers, mocking, CI gate |
| 16 | [16-EDGE-CASES.md](16-EDGE-CASES.md) | Comprehensive edge-case catalogue (API/queue/worker/provider/DB/Redis/scale) |
| 17 | [17-EVAL-SPECIFICATION.md](17-EVAL-SPECIFICATION.md) | Eval framework: structure, categories, graders, gates |
| 18 | [18-SECURITY.md](18-SECURITY.md) | AuthN/AuthZ, secrets, encryption, PII, log redaction |
| 19 | [19-DOCKER.md](19-DOCKER.md) | Dockerfile, docker-compose, multi-service topology, health checks |
| 20 | [20-DEPLOYMENT.md](20-DEPLOYMENT.md) | Dev → Docker → staging → production, secrets, scaling |
| 21 | [21-SCALABILITY.md](21-SCALABILITY.md) | Horizontal scaling path from 10 to 1M+ users |
| 22 | [22-DISASTER-RECOVERY.md](22-DISASTER-RECOVERY.md) | Failure recovery, backups, RPO/RTO targets |
| 23 | [23-CI-CD.md](23-CI-CD.md) | CI/CD pipeline design with 90% coverage gate |
| 24 | [24-IMPLEMENTATION-PLAN.md](24-IMPLEMENTATION-PLAN.md) | Phased implementation plan (0 → 14) |
| 25 | [25-REDIS-DESIGN.md](25-REDIS-DESIGN.md) | Redis responsibilities: rate limiting, idempotency cache, queues |
| 26 | [26-AUTH-AUDIT-DESIGN.md](26-AUTH-AUDIT-DESIGN.md) | Full authentication, authorization, audit design (detailed) |
| 27 | [27-TEST-PLAN.md](27-TEST-PLAN.md) | Detailed test-case matrix (TC-001..TC-150) |
| — | [BASELINE.md](BASELINE.md) | Pre-redesign baseline: architecture, test status, coverage |
| — | [00-README-PROJECT.md](00-README-PROJECT.md) | Original project README (preserved) |

## Evals

| File | Purpose |
| ---- | ------- |
| [evals/functional.yaml](evals/functional.yaml) | Functional correctness |
| [evals/concurrency.yaml](evals/concurrency.yaml) | Concurrent request handling |
| [evals/multi_user.yaml](evals/multi_user.yaml) | Multi-user ownership and isolation |
| [evals/reliability.yaml](evals/reliability.yaml) | Provider/queue/worker failure resilience |
| [evals/idempotency.yaml](evals/idempotency.yaml) | Duplicate send prevention |
| [evals/retry.yaml](evals/retry.yaml) | Retry policy, backoff, DLQ |
| [evals/scheduling.yaml](evals/scheduling.yaml) | Send-at, timezone, quiet hours |
| [evals/authorization.yaml](evals/authorization.yaml) | AuthN/AuthZ enforcement |
| [evals/security.yaml](evals/security.yaml) | Secret protection, PII, injection |
| [evals/observability.yaml](evals/observability.yaml) | Health, audit, logs, correlation |
| [evals/regression.yaml](evals/regression.yaml) | Regression (all 3 channels, duplicate, authz, secrets) |
| [evals/performance.yaml](evals/performance.yaml) | Latency, throughput, queue depth |

## Current Status (confirmed from the repository)

| Channel | Status | Provider(s) |
| ------- | ------ | ----------- |
| SMS | working | Vonage (preferred), Azure (fallback) |
| WhatsApp | working | Vonage Sandbox (preferred), Azure (fallback) |
| Email | working | Azure |

## Key Metrics

- **Coverage**: >= 90%
- **Tests**: all passing
- **Architecture**: API + PostgreSQL + Redis Streams + Workers + Provider abstraction
- **Container**: Dockerfile + docker-compose with API, worker (x3), PostgreSQL, Redis
- **CI/CD**: defined in [23-CI-CD.md](23-CI-CD.md)
- **Eval target**: critical evals 100%, overall >= 95%

## Principles (binding)

1. API validates and enqueues; it does not perform slow provider delivery inline.
2. Workers perform provider delivery.
3. PostgreSQL is the durable source of truth.
4. Redis supports but never replaces PostgreSQL.
5. Provider calls are isolated behind the provider interface.
6. Retries distinguish temporary vs permanent failures.
7. Idempotency prevents duplicate sends as far as the provider allows.
8. Queue messages are safe to redeliver (at-least-once semantics).
9. API servers and workers scale independently and horizontally.
10. Provider failures never crash the system; one broken channel never breaks others.
11. Secrets are never committed and never appear in logs.
12. Automated tests never send real notifications.
13. Coverage target is >= 90%.
14. Containers are independently deployable.
15. The application supports graceful shutdown.
16. New channels are added via the provider interface without rewriting the core.