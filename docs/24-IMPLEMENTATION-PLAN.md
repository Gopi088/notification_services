# 19 — Implementation Plan

Phased plan. Each phase: goal, files affected, dependencies, tasks, tests,
acceptance criteria, rollback.

## Phase 0 — Documentation & Architecture

- **Goal:** frozen architecture; these docs are the source of truth.
- **Files:** `docs/*`, `TEST_PLAN.md`, `AUTH_AUDIT_DESIGN.md`.
- **Deps:** none.
- **Tasks:** review docs; resolve contradictions; baseline coverage of current code.
- **Tests:** baseline `pytest` run on existing `test_*.py` scripts.
- **Acceptance:** docs approved; baseline coverage measured.
- **Rollback:** n/a (docs only).

## Phase 1 — Refactor Current Code (no behavior change)

- **Goal:** prepare structure for async/DB/queue without breaking channels.
- **Files:** `app/config.py`, `app/providers/*`, `app/orchestrator.py`, `app/database.py`.
- **Deps:** Phase 0.
- **Tasks:** introduce config properties for new env names; extract provider
  interface cleanly (already largely done); add request-id plumbing.
- **Tests:** keep existing provider/API tests green.
- **Acceptance:** all current tests pass; channels still work end-to-end in MOCK_MODE.
- **Rollback:** revert refactor; single commit.

## Phase 2 — PostgreSQL

- **Goal:** replace SQLite with PostgreSQL as source of truth.
- **Files:** `app/database.py`, `requirements.txt`, `app/config.py`, migrations.
- **Deps:** Phase 1.
- **Tasks:** SQLAlchemy/asyncpg or psycopg; schema (03); state machine; migrations tool.
- **Tests:** DB tests (03/10); API regression against PG.
- **Acceptance:** state machine + constraints enforced; SQLite removed from runtime.
- **Rollback:** keep SQLite path behind flag during migration window.

## Phase 3 — Message Queue (Redis Streams)

- **Goal:** enqueue notifications; decouple API from delivery.
- **Files:** `app/queue.py` (new), `app/config.py`, API `send`/`event`.
- **Deps:** Phase 2; Redis.
- **Tasks:** producer publish; stream naming; reconciliation job for orphaned `queued`.
- **Tests:** queue tests (04/10); redelivery + DLQ.
- **Acceptance:** API returns 202 after XADD; background dispatch removed.
- **Rollback:** flag `QUEUE_ENABLED=false` → in-process dispatch (temporary).

## Phase 4 — Worker

- **Goal:** standalone worker consuming and delivering.
- **Files:** `worker.py` (new), `app/worker.py`, `app/orchestrator.py` (trim), Docker Compose.
- **Deps:** Phase 3.
- **Tasks:** consumer group loop; optimistic `processing`; ack; graceful shutdown; concurrency.
- **Tests:** worker tests (05/10); crash + redelivery + idempotency no-op.
- **Acceptance:** worker delivers; API no longer sends inline.
- **Rollback:** run worker in-process via `QUEUE_ENABLED=false` + inline worker thread.

## Phase 5 — Provider Abstraction Completion

- **Goal:** providers fully isolated behind interface with status + retryable info.
- **Files:** `app/providers/base.py`, `app/providers/*`, factory.
- **Deps:** Phase 1.
- **Tasks:** add `is_retryable`, `get_status`, `validate`; error mapping (06).
- **Tests:** provider unit tests (06/10) incl. error mapping.
- **Acceptance:** new-channel mock provider added in a test proves extensibility.
- **Rollback:** n/a (additive interface).

## Phase 6 — Retry + Idempotency

- **Goal:** retryable/non-retryable split; no double-send.
- **Files:** `app/retry.py`, `app/idempotency.py`, `app/database.py`, API middleware.
- **Deps:** Phases 2–5.
- **Tasks:** backoff+jitter; retry stream; DLQ; idempotency keys (Redis + PG); timeout-verify.
- **Tests:** retry/idempotency tests (07/10); duplicate request; crash-after-send.
- **Acceptance:** duplicate API calls don't double-send; retries honor backoff; DLQ populated.
- **Rollback:** disable retries (`MAX_ATTEMPTS=1`) and idempotency flag.

## Phase 7 — Redis

- **Goal:** Redis integrated for rate limiting + idempotency cache (queue already uses streams).
- **Files:** `app/redis.py`, `app/config.py`.
- **Deps:** Phase 3.
- **Tasks:** connection mgmt; key schemas (09); fail-open behavior.
- **Tests:** Redis tests (fakeredis); failure behavior.
- **Acceptance:** rate limit + idempotency cache functional; Redis-down fails open.
- **Rollback:** `REDIS_ENABLED=false` → DB-only idempotency, no rate limit.

## Phase 8 — Rate Limiting

- **Goal:** enforce limits per key/recipient/channel/provider.
- **Files:** `app/ratelimit.py`, API + worker enforcement, middleware.
- **Deps:** Phase 7.
- **Tasks:** sliding window; headers; 429 + Retry-After; worker provider throttle.
- **Tests:** rate-limit tests (08/10); burst scenarios.
- **Acceptance:** limits enforced; bursts shed at edge; headers present.
- **Rollback:** `RATELIMIT_ENABLED=false`.

## Phase 9 — Observability

- **Goal:** structured logs, metrics, health/readiness/liveness.
- **Files:** `app/logging_config.py`, `app/middleware.py`, `app/metrics.py`, health endpoints.
- **Deps:** Phases 1–8.
- **Tasks:** JSON logs + request_id; Prometheus metrics; readiness checks; audit (AUTH_AUDIT_DESIGN).
- **Tests:** observability tests (13/10); redaction tests.
- **Acceptance:** metrics endpoint; readiness gates traffic; logs correlate.
- **Rollback:** n/a (additive).

## Phase 10 — Testing

- **Goal:** ≥ 90% coverage.
- **Files:** `tests/*`.
- **Deps:** Phases 1–9.
- **Tasks:** implement all TC-xxx; coverage gate; mocking strategy (10/10, TEST_PLAN).
- **Acceptance:** `--cov-fail-under=90` passes.
- **Rollback:** n/a.

## Phase 11 — Docker

- **Goal:** runnable via Docker Compose (API + worker + PG + Redis).
- **Files:** `Dockerfile`, `.dockerignore`, `docker-compose.yml`.
- **Deps:** Phases 2–4 (DB, queue, worker).
- **Tasks:** multi-stage build; health checks; startup order; non-root.
- **Tests:** Docker smoke (14/10).
- **Acceptance:** `docker compose up` brings all services healthy.
- **Rollback:** previous images.

## Phase 12 — CI/CD

- **Goal:** pipeline with 90% gate, security, build, deploy staging/prod.
- **Files:** `.github/workflows/*`, `Dockerfile`.
- **Deps:** Phase 10 (tests), Phase 11 (build).
- **Tasks:** lint, test, coverage gate, security scan, build/push, staging deploy, prod gate.
- **Acceptance:** PR blocks on coverage < 90%; merge auto-deploys staging.
- **Rollback:** revert workflow.

## Phase 13 — Deployment

- **Goal:** production deployment (Docker Compose VM or managed services).
- **Files:** deploy config, secrets manager integration.
- **Deps:** Phase 11–12.
- **Tasks:** env config, secrets, TLS/LB, rolling deploy, readiness.
- **Acceptance:** production serving traffic; channels verified with sandbox/test numbers.
- **Rollback:** previous release tag.

## Phase 14 — Load Testing

- **Goal:** validate scalability targets.
- **Files:** `loadtest/` (locust/k6).
- **Deps:** Phase 13.
- **Tasks:** ramp tests (100 → 10k → 1M requests); provider-quota checks; queue lag.
- **Acceptance:** meets targets without provider overrun; no regressions.
- **Rollback:** n/a (test-only).

## Cross-Cutting

- Every phase lands behind feature flags where noted, keeping the system
  deployable and reversible.
- Existing channels (SMS/WhatsApp/Email) remain working at every phase.