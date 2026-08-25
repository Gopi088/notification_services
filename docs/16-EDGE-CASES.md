# 11 — Edge Cases

Comprehensive catalogue. For each case: trigger, expected behavior, response,
persistence, retry, logging, alerting.

## 11.1 API Edge Cases

| # | Trigger | Expected behavior | HTTP | Persistence | Retry | Log/Alert |
| - | ------- | ----------------- | ---- | ----------- | ----- | --------- |
| E-API-01 | Empty request body | 422 schema error | 422 | none | no | info |
| E-API-02 | Malformed JSON | 422 | 422 | none | no | info |
| E-API-03 | Missing `channels` | 422 | 422 | none | no | info |
| E-API-04 | Empty `channels` array | 422 | 422 | none | no | info |
| E-API-05 | `null` field | 422 | 422 | none | no | info |
| E-API-06 | Wrong type (contact=123) | 422 | 422 | none | no | info |
| E-API-07 | Unknown extra field | Accepted (extra ignored) | 202 | queued | no | debug |
| E-API-08 | Huge payload (> limit) | 422 / 413 | 422/413 | none | no | warn |
| E-API-09 | Invalid channel `fax` | 422 | 422 | none | no | info |
| E-API-10 | Invalid recipient phone | 400 validation_error | 400 | none | no | info |
| E-API-11 | Invalid email | 400 | 400 | none | no | info |
| E-API-12 | Duplicate channel in one request | 422 | 422 | none | no | info |
| E-API-13 | Duplicate request (same Idempotency-Key) | return original 202, `X-Idempotent-Replay: true` | 202 | unchanged | no | info |
| E-API-14 | Duplicate request, different payload | 409 idempotency_conflict | 409 | none | no | warn |
| E-API-15 | Message exactly 4096 chars | accepted | 202 | queued | — | info |
| E-API-16 | Message 4097 chars | 422 | 422 | none | no | info |
| E-API-17 | Whitespace-only message | 422 | 422 | none | no | info |
| E-API-18 | Unicode / emoji message | accepted, stored UTF-8 | 202 | queued | — | info |
| E-API-19 | `scheduled_at` in the past | treat as send-now (queued immediately) | 202 | queued | — | info |
| E-API-20 | `scheduled_at` far future (> retention) | accepted if within horizon, else 422 | 202/422 | queued | — | info |
| E-API-21 | Leading-zero phone `0-98872-70348` | normalized to `+919887270348` | 202 | queued | — | info |
| E-API-22 | Phone with spaces/hyphens | normalized via `_normalize_phone` | 202 | queued | — | info |
| E-API-23 | 11-digit phone starting with country code | treated as E.164 best-effort | 202/400 | per validation | — | info |
| E-API-24 | `Idempotency-Key` header present but empty | treat as no key (or 422 per policy) | 202/422 | queued | — | info |
| E-API-25 | Auth disabled + AUTH_API_KEY empty | requests allowed (dev) | 202 | queued | — | debug |
| E-API-26 | AUTH_ENABLED=true + AUTH_API_KEY empty | 500 server_config_error | 500 | none | no | error/alert |

## 11.2 Queue Edge Cases

| # | Trigger | Expected behavior | Retry | Log/Alert |
| - | ------- | ----------------- | ----- | --------- |
| E-Q-01 | Duplicate message in stream | idempotency no-op; ACK | no | info |
| E-Q-02 | Message lost (Redis crash before AOF flush) | reconciliation re-enqueues orphaned `queued` rows | yes | warn/alert |
| E-Q-03 | Redelivery after consumer crash | reclaimed via XAUTOCLAIM; idempotency guards double-send | yes | warn |
| E-Q-04 | Producer crashes after XADD | message processed normally | — | info |
| E-Q-05 | Consumer crashes after XREADGROUP | pending message reassigned after visibility timeout | yes | alert |
| E-Q-06 | Queue (Redis) unavailable | API returns 503 on enqueue; workers retry read | — | alert |
| E-Q-07 | Malformed queue message | DLQ with `dlq_reason=malformed`; alert | no | alert |
| E-Q-08 | Poison message (always fails) | retries then DLQ | up to max | alert |
| E-Q-09 | Ordering across channels | best-effort; per-channel stream preserves intra-channel order | — | — |
| E-Q-10 | Backlog builds | depth/lag metric; new enqueues for channel get 429 backpressure | — | alert |
| E-Q-11 | Message exceeds stream size limit | rejected at publish; API returns 413/422; never truncates | no | warn |
| E-Q-12 | Stream entry retention / trimming | capped via MAXLEN; old entries pruned (PostgreSQL is authority) | — | — |
| E-Q-13 | Consumer group does not exist | `XGROUP CREATE ... MKSTREAM` on worker startup | — | warn once |

## 11.3 Worker Edge Cases

| # | Trigger | Expected behavior | Retry | Log/Alert |
| - | ------- | ----------------- | ----- | --------- |
| E-W-01 | Worker crash mid-send | message reclaimed; idempotency prevents double-send | yes | alert |
| E-W-02 | Graceful shutdown (SIGTERM) | finish in-flight ≤ grace, then exit | — | info |
| E-W-03 | Provider timeout | retryable classification; backoff | yes | warn |
| E-W-04 | Provider accepts but response lost | verify via get_status if available; else mark uncertain; may double-send (documented residual) | no resend if verified | warn |
| E-W-05 | Database failure | retry DB op, do not ACK until persisted; alert after N | yes | alert |
| E-W-06 | Redis failure | rate limit fails open; queue consumers retry | — | alert |
| E-W-07 | Two workers race same message | optimistic `processing` guard; loser ACKs and skips | no | debug |
| E-W-08 | Malformed DB row / missing notification | DLQ + alert | no | alert |
| E-W-09 | Worker stuck in `processing` (crash before update) | reclaim via XAUTOCLAIM; optimistic guard; reconciliation marks stale `processing` rows | yes | alert |
| E-W-10 | Provider returns success but no message id | treat as uncertain; attempt get_status; else record error | no resend | warn |
| E-W-11 | `next_attempt_at` reached but worker backlog | retry consumer picks up when due; lag metric alerts | — | warn |
| E-W-12 | Worker terminates during XACK (after status persisted) | message redelivered; idempotency sees `submitted` → no-op ACK | no | debug |

## 11.4 Provider Edge Cases

Only statuses that apply to Vonage/Azure:

| Code | Meaning | Retryable |
| ---- | ------- | --------- |
| 400 | invalid request | No |
| 401 | invalid credentials | No |
| 403 | forbidden / sandbox not allow-listed | No |
| 404 | recipient not found / channel invalid | No |
| 408 | request timeout | Yes |
| 409 | conflict | No |
| 422 | validation | No |
| 429 | rate limited | Yes (honor Retry-After) |
| 500 | provider error | Yes |
| 502 | bad gateway | Yes |
| 503 | service unavailable | Yes |
| 504 | gateway timeout | Yes |
| network | connection error | Yes |
| empty response | no body | Depends (query status) |
| missing message id | provider acked without id | Uncertain (verify) |

## 11.5 Database Edge Cases

| # | Trigger | Expected behavior | Log/Alert |
| - | ------- | ----------------- | --------- |
| E-DB-01 | Connection failure | API 503 on write; worker retries | alert |
| E-DB-02 | Timeout | same as connection failure | warn |
| E-DB-03 | Duplicate primary key | integrity error surfaced; idempotent insert handles key conflict | info |
| E-DB-04 | Transaction failure | rollback; status unchanged; retry | warn |
| E-DB-05 | Deadlock | retry with backoff (bounded) | warn |
| E-DB-06 | DB unavailable | service degrades; queue keeps messages; reconciliation on recovery | alert |

## 11.6 Redis Edge Cases

| # | Trigger | Expected behavior | Log/Alert |
| - | ------- | ----------------- | --------- |
| E-R-01 | Redis unavailable | rate limit fails open; idempotency falls back to PG; queue consumers retry | alert |
| E-R-02 | Redis timeout | treated like unavailable (fail-open) | warn |
| E-R-03 | Cache miss | fall back to PostgreSQL | debug |
| E-R-04 | Stale cache | idempotency reads PG for authority; status cached only with short TTL + invalidation | debug |
| E-R-05 | Key expiration | re-created on demand; idempotency verified in PG | debug |
| E-R-06 | Memory pressure | LRU evicts cache/rate keys; streams unaffected; watch/monitor | warn/alert |

## 11.7 Retry Edge Cases

| # | Trigger | Expected behavior |
| - | ------- | ----------------- |
| E-RT-01 | Retry storm (batch failure) | jitter + per-channel concurrency cap + circuit breaker |
| E-RT-02 | Max attempts reached | DLQ + `dead_lettered` status + alert |
| E-RT-03 | Backoff overflow | capped at `max_delay` |
| E-RT-04 | Jitter range | delay within `[0.8x, 1.2x]` |
| E-RT-05 | Retrying a non-retryable error | blocked by classification; straight to failed/DLQ |

## 11.8 Idempotency Edge Cases

| # | Trigger | Expected behavior |
| - | ------- | ----------------- |
| E-I-01 | Duplicate API request | return original 202 |
| E-I-02 | Duplicate queue message | no-op ACK |
| E-I-03 | Worker crash after send | get_status/verify; no resend if confirmed |
| E-I-04 | Provider timeout after acceptance | uncertain window; verify; document residual double-delivery |
| E-I-05 | Idempotency-Key too long / malformed | reject 422 (key max length enforced); never accept unbounded keys |
| E-I-06 | Idempotency-Key with non-ASCII / whitespace | normalize (trim, lowercase); reject control characters |
| E-I-07 | Same key, conflicting payload | 409 idempotency_conflict; no enqueue |
| E-I-08 | Idempotency key TTL shorter than retry horizon | worker re-verifies against PostgreSQL before sending; never trusts Redis cache alone |
| E-I-09 | Redis idempotency cache evicted early | fall back to PostgreSQL `idempotency_keys` (durable) |
| E-I-10 | Key collision (hash) | SHA-256 collision practically impossible; use full-key comparison to be safe |

## 11.9 Scale Edge Cases

| Volume | Expected behavior |
| ------ | ----------------- |
| 1 request | normal path |
| 100 concurrent | within limits; all processed |
| 10,000 concurrent | 429 for over-limit; queue absorbs rest |
| 1,000,000 concurrent | API scales horizontally; Redis shards; provider rate caps egress; fast reject at edge |

## 11.10 Webhook Edge Cases

| # | Trigger | Expected behavior |
| - | ------- | ----------------- |
| E-WH-01 | Duplicate webhook event (provider retries delivery) | idempotent: status update applied once; repeated event is a no-op (already delivered) |
| E-WH-02 | Out-of-order events (failed arrives after delivered) | `delivered` is terminal; ignore regressions to failed for the same provider_message_id |
| E-WH-03 | Webhook for unknown provider_message_id | record to `webhook_events` (raw); cannot map to a notification; log warning, no crash |
| E-WH-04 | Malformed / non-JSON webhook payload | return 400; log; no DB change |
| E-WH-05 | Invalid HMAC signature | 401; audit `webhook.auth_failed`; no status change |
| E-WH-06 | Webhook arrives after status timed_out | process normally; `delivered`/`failed` still update the notification |
| E-WH-07 | Webhook for a channel other than whatsapp | ignored (already handled: non-whatsapp events skipped) |
| E-WH-08 | Event Grid validation event | return `{"validationResponse": code}`; audit; no DB change |
| E-WH-09 | Large / batched webhook payload | process each event; bounded loop; never block request indefinitely |
| E-WH-10 | Webhook flood (retry storm from provider) | rate-limit webhook endpoint; idempotent processing prevents status churn |

## 11.11 Concurrency & Ordering Edge Cases

| # | Trigger | Expected behavior |
| - | ------- | ----------------- |
| E-C-01 | Two workers race to claim same message | optimistic `processing` UPDATE guard; loser ACKs and skips |
| E-C-02 | Webhook updates status while worker is sending | both use atomic UPDATE; last-writer-wins on `updated_at`; no double state corruption |
| E-C-03 | Reconcile job races with active worker | reconcile only touches rows still `queued`/`retrying` with `next_attempt_at` passed |
| E-C-04 | Clock skew between API / worker / provider | use UTC timestamps everywhere; backoff computed from DB time, not local clock |
| E-C-05 | Message ordering across retries | retried message may overtake newer ones; ordering is best-effort, never guaranteed |
| E-C-06 | Duplicate notification for same recipient+content (no client key) | server-derived idempotency key from `(channel, recipient, message_hash, reference)` dedupes |

For every case, persistence/status behavior is recorded per the state machine in
[03-DATA-MODEL.md](03-DATA-MODEL.md), and logging/alerting follows
[14-OBSERVABILITY.md](14-OBSERVABILITY.md).