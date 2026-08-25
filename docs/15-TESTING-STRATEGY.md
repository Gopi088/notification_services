# 10 — Testing Strategy

## 10.1 Coverage Target

```
Statements >= 90%
Branches   >= 90%
Functions  >= 90%
Lines      >= 90%
Overall    >= 90%
```

Coverage gate: CI fails if overall coverage < 90%
(`--cov-fail-under=90`). See [TEST_PLAN.md](27-TEST-PLAN.md) for the detailed
test-case matrix (TC-001 … TC-150+).

## 10.2 Test Layers

| Layer | Scope | External calls |
| ----- | ----- | -------------- |
| Unit | Functions/classes: validation, config, templates, phone normalization, providers, database helpers, redaction, retry math | mocked |
| Integration | Route → orchestrator → provider (mocked) → DB; webhook → DB | mocked |
| API | FastAPI `TestClient` for every endpoint, positive/negative/boundary | mocked |
| Provider | Payload shape, response parsing, error mapping for each provider | mocked |
| Queue | Producer publish, consumer group ack, redelivery, DLQ | real Redis in tests OR fakeredis |
| Worker | Consume → send (mocked) → status update → ack; crash/retry paths | mocked provider, real/tmp DB |
| Database | CRUD, state machine transitions, unique constraints, transactions | tmp PostgreSQL (testcontainers) or SQLite-compatible subset |
| Redis | Rate limit, idempotency cache, stream groups | fakeredis or real Redis container |
| Retry | Backoff math, jitter bounds, retryable classification, DLQ routing | mocked |
| Idempotency | Duplicate request, redelivery, crash-after-send | mocked |
| Rate limiting | Sliding window, headers, 429, burst | fakeredis |
| Security | AuthN/AuthZ, secret redaction, SSRF guards, injection, PII masking | mocked |
| Docker | Image builds, container starts, health checks | real containers in CI stage |

**Rule:** automated tests **never** send real SMS/WhatsApp/email. Every provider
call is mocked.

## 10.3 Mocking

- Vonage SMS: mock SDK `client.messages.send`.
- Vonage WhatsApp: mock `requests.post` in `vonage_provider`.
- Azure SMS/Email/WhatsApp: mock SDK clients (`from_connection_string`, `send`,
  `begin_send`, `NotificationMessagesClient`).
- Attachment downloads: mock `httpx.stream`.
- Redis: `fakeredis` (unit/integration) or disposable container (queue tests).
- PostgreSQL: disposable container via `testcontainers` (or SQLite for logic-only
  unit tests, with a documented parity caveat).

## 10.4 Test Organization

```
tests/
├── conftest.py            # fixtures: app client, tmp DB, env patch, mock providers, redis
├── unit/                  # config, validation, templates, normalize, providers, db helpers
├── integration/           # lifecycle, webhook→db, queue→worker→db
├── api/                   # send, event, status, legacy, health, webhook endpoints
├── providers/             # per-provider payload/error tests
├── queue/                 # streams, ack, dlq, redelivery
├── worker/                # consume/send/ack, crash, retry
├── redis/                 # rate limit, idempotency cache
├── security/              # authn/authz, redaction, ssrf
└── retry/                 # backoff, jitter, classification
```

## 10.5 Test Commands

```bash
# all
venv/bin/python -m pytest tests/ -v
# unit
venv/bin/python -m pytest tests/unit -v
# coverage + gate
venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=90
# html report
venv/bin/python -m pytest tests/ --cov=app --cov-report=html
```

## 10.6 Detailed Test Matrix (summary)

| Area | Key cases |
| ---- | --------- |
| API send | valid per channel, multi-channel, missing/invalid fields, 400/401/403/404/409/422/429/503 |
| API status | group, single, not found, delivered/failed, timed_out |
| Event | valid envelope, empty deliveries, bad payload |
| Providers | success, payload correctness, auth error, timeout, network, malformed response, message-id extraction, secret non-exposure |
| Worker | consume→send→ack, provider fail retry, DLQ, crash redelivery, idempotency no-op, graceful shutdown |
| Queue | publish, ack, redelivery, duplicate, DLQ, backlog |
| Retry | backoff bounds, jitter, max attempts, non-retryable shortcut |
| Idempotency | duplicate request, redelivery, crash-after-send, conflicting key 409 |
| Rate limit | window, headers, 429, burst, fail-open on Redis down |
| Database | CRUD, state machine, constraints, migration |
| Security | authN/authZ, redaction, SSRF, injection, PII |
| Edge cases | see [16-EDGE-CASES.md](16-EDGE-CASES.md) |

Full TC-xxx matrix: see [TEST_PLAN.md](27-TEST-PLAN.md).