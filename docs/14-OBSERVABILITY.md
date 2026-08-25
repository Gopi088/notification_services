# 13 — Observability

## 13.1 Principles

- Structured logs (JSON) with correlation IDs.
- Metrics exported for dashboards (Prometheus format).
- Health endpoints differentiate liveness vs readiness.
- Never log secrets or full message content.

## 13.2 Logging

**Structured log fields:**

| Field | Example |
| ----- | ------- |
| `ts` | ISO-8601 UTC |
| `level` | info/warning/error |
| `logger` | module |
| `request_id` | `req_4f1a...` |
| `notification_id` | UUID |
| `group_id` | UUID |
| `channel` | whatsapp/sms/email |
| `provider` | vonage_whatsapp |
| `status` | queued/processing/submitted/delivered/failed/dead_lettered |
| `attempt` | 1..5 |
| `provider_message_id` | uuid |
| `latency_ms` | integer |
| `error_code` / `error_message` | provider codes |
| `actor` | api key id |

**Never log:**

- API secrets / connection strings / access keys.
- Full `X-API-Key` header.
- Raw webhook signature.
- Full message bodies by default (email subject only at DEBUG).
- PII beyond need (masked phones).

**Log redaction:** central `_redact()` (reused from `app/routers/webhooks.py`)
masks keys like `token`, `secret`, `password`, `authorization`, `key`.

**Correlation:** `request_id` propagated from API → queue message → worker → DB
rows (`request_id` column) → webhook reconciliation.

## 13.3 Metrics (Prometheus)

| Metric | Type | Labels |
| ------ | ---- | ------ |
| `notifications_requested_total` | counter | channel, key |
| `notifications_queued_total` | counter | channel |
| `notifications_processed_total` | counter | channel, provider |
| `notifications_success_total` | counter | channel, provider |
| `notifications_failure_total` | counter | channel, provider, error_code |
| `notifications_retry_total` | counter | channel, attempt |
| `notifications_dead_lettered_total` | counter | channel |
| `provider_request_duration_seconds` | histogram | provider, operation |
| `provider_errors_total` | counter | provider, error_code |
| `queue_depth` | gauge | stream/channel |
| `queue_lag` | gauge | consumer group |
| `worker_utilization` | gauge | worker id, channel |
| `api_request_duration_seconds` | histogram | path, method |
| `api_errors_total` | counter | path, status |
| `redis_available` | gauge | — |
| `db_pool_available` | gauge | — |
| `idempotency_hits_total` / `idempotency_misses_total` | counter | — |
| `ratelimit_rejected_total` | counter | bucket |

**Alerting thresholds (examples):**

- `notifications_failure_total` rate spike > baseline × 5 over 5 min.
- `notifications_dead_lettered_total` > 0 in 15 min.
- `queue_lag` > N for 5 min.
- `provider_error_rate` > 20% over 5 min.
- `redis_available == 0` or `db_pool_available == 0`.

## 13.4 Health Checks

| Endpoint | Type | Checks | Response |
| -------- | ---- | ------ | -------- |
| `GET /health` | Liveness | process responsive | always 200 `{"status":"ok","version":...}` |
| `GET /api/v1/health/liveness` | Liveness | process alive, no dependencies | 200 |
| `GET /api/v1/health/readiness` | Readiness | PostgreSQL reachable, Redis reachable, queue reachable | 200 if all; 503 with per-dependency detail |

**Difference:** liveness = "am I up?" (probe restarts if dead). Readiness = "am I
ready to accept traffic?" (LB drains if 503).

- API readiness: DB + Redis + queue.
- Worker readiness: DB + Redis (queue connection) reachable.
- MOCK_MODE reports dependencies as ready with `mock: true`.

## 13.5 Distributed Tracing (optional)

- If the stack adds Jaeger/OTel: propagate `traceparent` from `X-Request-ID`
  request; spans: `api.validate`, `api.enqueue`, `worker.process`,
  `provider.send`, `db.query`. Keep optional and additive.