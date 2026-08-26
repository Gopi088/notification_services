# Test Plan — Lifecycle, Status, Queue, Reliability

## 5.1 Purpose

Comprehensive test plan for the notification lifecycle, status/acknowledgement,
queue architecture, reliability, and edge cases. Covers unit, integration, and
API tests. Target >= 90% coverage.

## 5.2 Lifecycle State Machine Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| LIFECYCLE-001 | Create notification → status `queued` | row created, status queued |
| LIFECYCLE-002 | `queued → processing` | legal transition |
| LIFECYCLE-003 | `processing → submitted` | legal transition |
| LIFECYCLE-004 | `submitted → delivered` | legal transition |
| LIFECYCLE-005 | `submitted → failed` | legal transition |
| LIFECYCLE-006 | `delivered → read` | legal transition |
| LIFECYCLE-007 | `delivered → acknowledged` | legal transition |
| LIFECYCLE-008 | `read → acknowledged` | legal transition |
| LIFECYCLE-009 | Invalid transition `queued → delivered` | rejected, state unchanged |
| LIFECYCLE-010 | Invalid transition `delivered → processing` | rejected |
| LIFECYCLE-011 | `failed → retrying` | legal transition |
| LIFECYCLE-012 | `retrying → processing` | legal transition |
| LIFECYCLE-013 | `failed → dead_lettered` | legal transition |
| LIFECYCLE-014 | `queued → cancelled` | legal transition |
| LIFECYCLE-015 | `scheduled → queued` | legal transition |
| LIFECYCLE-016 | `queued → expired` | legal transition |
| LIFECYCLE-017 | Provider `delivered` webhook → `delivered` | status updated |
| LIFECYCLE-018 | Provider `read` webhook → `read` | status updated |
| LIFECYCLE-019 | Provider `failed` webhook → `failed` | error stored |

## 5.3 Status API Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| STATUS-001 | POST then immediate GET /status | status `queued` or `processing` |
| STATUS-002 | GET /status for a single message | correct status |
| STATUS-003 | GET /status for a group | all channels, correct status |
| STATUS-004 | GET /status for unknown id | 404 |
| STATUS-005 | GET /status after delivered | status `delivered` |
| STATUS-006 | GET /status includes `read_at` | null before read, set after |
| STATUS-007 | GET /status includes `acknowledged_at` | null before ack, set after |
| STATUS-008 | Concurrent GET /status requests | no race, consistent result |
| STATUS-009 | GET /status during webhook processing | consistent (no partial) |

## 5.4 Webhook / Provider Status Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| WEBHOOK-001 | Delivered webhook → status updated | DB row updated |
| WEBHOOK-002 | Read webhook → status updated | DB row updated |
| WEBHOOK-003 | Failed webhook → status + error stored | DB row updated |
| WEBHOOK-004 | Duplicate webhook (same provider_message_id) | no-op (idempotent) |
| WEBHOOK-005 | Webhook for unknown notification_id | recorded, ignored |
| WEBHOOK-006 | Webhook arrives after terminal state | no-op |
| WEBHOOK-007 | Webhook arrives before status response | consistent read |
| WEBHOOK-008 | Malformed webhook payload | 400, no crash |
| WEBHOOK-009 | Webhook retry (provider resends) | idempotent |

## 5.5 Acknowledgement Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| ACK-001 | Acknowledge from `delivered` | transition to `acknowledged` |
| ACK-002 | Acknowledge from `read` | transition to `acknowledged` |
| ACK-003 | Acknowledge from `failed` | rejected (invalid transition) |
| ACK-004 | Duplicate acknowledgement | idempotent |
| ACK-005 | `acknowledged_at` timestamp set | correct |
| ACK-006 | `acknowledgement_type` stored | correct |

## 5.6 Inbound User Response Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| INBOUND-001 | User replies via SMS inbound | record stored, audit created |
| INBOUND-002 | User replies via WhatsApp inbound | record stored, audit created |
| INBOUND-003 | Unknown sender | stored, not linked to notification |
| INBOUND-004 | Duplicate response | stored (idempotent mapping) |
| INBOUND-005 | Response after acknowledgement | stored, no re-acknowledgement |
| INBOUND-006 | Malformed webhook | 400, no crash |
| INBOUND-007 | Provider webhook retry | idempotent |
| INBOUND-008 | Invalid signature | 401, no crash |

## 5.7 Queue & Worker Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| QUEUE-001 | Publish job → consumed → acked | successful delivery |
| QUEUE-002 | In-memory queue (QUEUE_BACKEND=memory) | job processed |
| QUEUE-003 | Worker crash mid-send → redelivery | idempotent no-op |
| QUEUE-004 | Retry routing → `retrying` → backoff | retry stream entry |
| QUEUE-005 | Dead-letter → `dead_lettered` | DLQ entry |
| QUEUE-006 | Queue unavailable → notification persisted | 503, no data loss |
| QUEUE-007 | Reconciliation re-enqueues `queued` rows | re-queued on startup |
| QUEUE-008 | Memory queue process restart → reconcile | orphaned rows re-enqueued |

## 5.8 Reliability Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| RELIABILITY-001 | Retryable failure → backoff → retry | attempts < max |
| RELIABILITY-002 | Non-retryable failure → `failed` | no retry |
| RELIABILITY-003 | Retries exhausted → `dead_lettered` | max attempts reached |
| RELIABILITY-004 | Idempotent duplicate API request | 202 replay, 1 provider call |
| RELIABILITY-005 | Duplicate queue message | no-op |
| RELIABILITY-006 | Provider timeout → retryable | status `retrying` |
| RELIABILITY-007 | Queue publish failure | notification persisted |
| RELIABILITY-008 | Database unavailable → API 503 | no silent loss |
| RELIABILITY-009 | Rate limit exceeded → 429 | not enqueued |

## 5.9 Audit Tests

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| AUDIT-001 | Every lifecycle transition creates audit | audit rows exist |
| AUDIT-002 | Webhook delivered → audit | `notification_delivered` |
| AUDIT-003 | Webhook read → audit | `notification_read` |
| AUDIT-004 | Acknowledgement → audit | `notification_acknowledged` |
| AUDIT-005 | Inbound response → audit | `user_response_received` |
| AUDIT-006 | Retry scheduled → audit | `retry_scheduled` |
| AUDIT-007 | Retry exhausted → audit | `retry_exhausted` |
| AUDIT-008 | Secrets never in audit | all audit tests pass |

## 5.10 Edge Cases

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| EDGE-001 | Immediate GET after POST | returns `queued`/`processing` |
| EDGE-002 | Duplicate POST with same idempotency key | 202 replay |
| EDGE-003 | Concurrent requests, same key | 1 notification |
| EDGE-004 | Worker crash between send and ack | redelivery, no double-send |
| EDGE-005 | Webhook arrives before status response | consistent read |
| EDGE-006 | Webhook after notification is `failed` | no-op (terminal) |
| EDGE-007 | Unknown webhook notification | recorded, ignored |
| EDGE-008 | Provider never sends delivery status | `submitted` → timeout |
| EDGE-009 | Memory queue restart → reconcile | orphaned rows re-enqueued |
| EDGE-010 | Invalid status transition | rejected, logged, audited |

## 5.11 Commands

```bash
# Run all tests
venv/bin/python -m pytest

# Tests with coverage
venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=90

# With Docker
docker compose up -d
curl http://localhost:8000/health
docker compose down
```
## 5.12 Concurrency Stress Tests (tests/test_concurrent.py)

| Test | Scenario | Expected |
| ---- | -------- | -------- |
| CONCURRENT-001 | 10 simultaneous users | all 202, all persisted, no duplicates |
| CONCURRENT-002 | 50 simultaneous users | all 202, no duplicates, audit=50 |
| CONCURRENT-003 | 100 simultaneous users | API doesn't crash, no data loss, no duplicates (SQLite may reject ~1; PG handles all) |
| CONCURRENT-004 | 100 requests same Idempotency-Key | exactly 1 notification created (DB unique mutex) |

Metrics measured: requests sent, accepted, rejected, queue depth, processing
time, retry count, successful/failed notifications.
