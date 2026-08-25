# 02 — API Specification

This document defines the complete API contract. It is backward compatible with
the existing API and extends it.

## 2.1 Base URL / Versioning

- Base path: `/api/v1`
- Content-Type: `application/json`
- All API responses share a uniform envelope.

## 2.2 Authentication

- Header: `X-API-Key: <key>`
- When `AUTH_ENABLED=false` (dev), authentication is skipped.
- When `AUTH_ENABLED=true`, all `/api/v1` endpoints except `/health` require a key.
- Webhook endpoints are authenticated by HMAC signature (`X-EventGrid-Notification-Signature`), not API key.
- 401 response: `{"success": false, "error": {"code": "unauthorized", "message": "...", "field": null}}`

## 2.3 Common Headers

| Header | Optional | Purpose |
| ------ | -------- | ------- |
| `X-API-Key` | required when AUTH_ENABLED | auth |
| `Idempotency-Key` | optional | client-supplied idempotency key (see 02.6) |
| `X-Request-ID` | optional | correlation id, echoed back |
| `Content-Type` | required | `application/json` |

## 2.4 Endpoints

### 2.4.1 POST `/api/v1/notifications/send`

Send one notification over one or more channels.

**Request body**

```json
{
  "channels": [
    {"channel": "whatsapp", "contact": "+919887270348", "template_name": "test_template", "template_language": "en", "template_params": [{"name": "body", "value": "Hi"}]},
    {"channel": "sms", "contact": "+919887270348"},
    {"channel": "email", "contact": "user@example.com"}
  ],
  "message": "Your interview has been confirmed.",
  "reference": "ORDER-123"
}
```

| Field | Type | Required | Notes |
| ----- | ---- | -------- | ----- |
| `channels[].channel` | enum | yes | `whatsapp`, `sms`, `email` (extensible) |
| `channels[].contact` | string | yes | phone (E.164-ish) or email |
| `channels[].template_name` | string | no | WhatsApp/email template |
| `channels[].template_language` | string | no | template language |
| `channels[].template_params` | array | no | template variable values |
| `message` | string | yes | 1..4096 chars |
| `reference` | string | no | caller reference, max 128 |

**Response 202**

```json
{
  "success": true,
  "message_id": "group-uuid",
  "reference": "ORDER-123",
  "status": "queued",
  "channels": [
    {"message_id": "msg-uuid-1", "channel": "whatsapp", "status": "queued", "contact": "+919887270348"},
    {"message_id": "msg-uuid-2", "channel": "sms", "status": "queued", "contact": "+919887270348"}
  ]
}
```

**Errors**: 400 validation, 401 auth, 403 forbidden (insufficient scope),
422 schema, 429 rate-limited, 503 queue/db unavailable.

### 2.4.2 POST `/api/v1/notifications/event`

Event-driven fan-out; each delivery carries its own payload.

```json
{
  "request_id": "req_9f8a3b2c",
  "event_type": "interview_confirmation",
  "ref": "7f21ab9c",
  "data": {"name": "Rahul"},
  "deliveries": [
    {"channel": "whatsapp", "payload": {"recipient": "+919887270348", "template": {"id": "test_template", "language": "en"}}},
    {"channel": "sms", "payload": {"recipient": "+919887270348", "message": "Your interview is confirmed"}},
    {"channel": "email", "payload": {"recipient": "a@b.com", "subject": "Interview", "message": "..."}}
  ]
}
```

### 2.4.3 GET `/api/v1/notifications/{notification_id}/status`

Poll delivery status for a group or single message.

```json
{
  "success": true,
  "message_id": "group-uuid",
  "reference": "ORDER-123",
  "status": "partial",
  "channels": [
    {"message_id": "msg-1", "channel": "whatsapp", "contact": "+919887270348",
     "status": "delivered", "provider": "vonage_whatsapp",
     "provider_message_id": "uuid", "error": null,
     "created_at": "...", "updated_at": "...",
     "elapsed_seconds": 4.2, "timed_out": false, "delivery_timeout_seconds": 300}
  ]
}
```

**Errors**: 404 unknown id, 401, 403.

### 2.4.4 GET `/api/v1/notifications/{notification_id}` (proposed)

Full notification detail (message, all attempts, timeline).

**Response (proposed):**

```json
{
  "success": true,
  "message_id": "notification-uuid",
  "group_id": "group-uuid",
  "channel": "whatsapp",
  "recipient": "+919887270348",
  "status": "submitted",
  "provider": "vonage_whatsapp",
  "provider_message_id": "uuid",
  "retry_count": 2,
  "created_at": "...",
  "updated_at": "...",
  "last_error": null,
  "attempts": [
    {"attempt": 1, "status": "submitted", "provider_message_id": "uuid-1", "duration_ms": 420, "created_at": "..."},
    {"attempt": 2, "status": "failed", "error_code": "408", "error_message": "timeout", "duration_ms": 30000, "created_at": "..."}
  ]
}
```

## 2.4.4b Future channels

The `channels[].channel` enum is extensible. New channels (e.g. `push`, `telegram`,
`slack`) are added by:

1. Extending the `Channel` enum and `channel` union in the schemas.
2. Adding a provider implementing the ABC and registering it in the factory.
3. Extending validation, rate-limit buckets, and worker concurrency config for the
   new channel.

No changes to the core send/event/status flow, queue, or worker are required — this
is the current design and remains true in the target architecture.

### 2.4.5 POST `/api/v1/notifications/{notification_id}/retry` (proposed)

Requeue a failed or dead-lettered notification.

- Only allowed when status is `failed`, `dead_lettered`, or `cancelled`.
- Requires scope `send:retry`.
- Returns `202` and resets status to `queued`, `retry_count` preserved or reset per policy.

### 2.4.5b POST `/api/v1/notifications/{notification_id}/cancel` (proposed)

Cancel a notification that has not yet been delivered.

- Allowed only when status is `queued`, `retrying`, or `processing`.
- Requires scope `send:cancel`.
- Sets status `cancelled`; the worker checks the status before sending and
  treats a `cancelled` row as a no-op (skips provider call, ACKs).
- Race: if a worker already sent, cancel returns `409 conflict` with current status.
- Returns `200 {"message_id": "...", "status": "cancelled"}` or `409` if already sent.

### 2.4.6 Health / Liveness / Readiness

| Endpoint | Purpose | Response |
| -------- | ------- | -------- |
| `GET /health` | Liveness — process is up | 200 `{"status":"ok","mock_mode":false,"version":"2.0.0"}` |
| `GET /api/v1/health/readiness` (proposed) | Readiness — DB + queue reachable | 200 or 503 with detail |
| `GET /api/v1/health/liveness` (proposed) | Liveness — process responsive | 200 always |

### 2.4.7 Legacy endpoints (kept)

| Endpoint | Notes |
| -------- | ----- |
| `POST /send` | single channel; maps to `/api/v1/notifications/send` |
| `GET /status/{message_id}` | maps to status lookup |

### 2.4.8 Webhook

| Endpoint | Purpose | Auth |
| -------- | ------- | ---- |
| `POST /api/v1/whatsapp/webhook` | delivery receipts + Event Grid validation | HMAC `X-EventGrid-Notification-Signature` |
| `GET /api/v1/whatsapp/webhook` | Event Grid validation handshake | public |

## 2.5 Error Format

Uniform envelope:

```json
{
  "success": false,
  "error": {
    "code": "validation_error",
    "message": "human-readable detail",
    "field": "channels[0].contact"
  }
}
```

| Code | HTTP | Meaning |
| ---- | ---- | ------- |
| `validation_error` | 400 | contact/input validation failed |
| `unauthorized` | 401 | missing/invalid API key |
| `key_expired` / `key_disabled` | 401 | key lifecycle |
| `webhook_signature_invalid` | 401 | bad HMAC on webhook delivery receipt |
| `forbidden` | 403 | valid key, insufficient scope |
| `not_found` | 404 | unknown id |
| `unprocessable_entity` | 422 | schema/type validation |
| `rate_limited` | 429 | throttled |
| `provider_unavailable` / `queue_unavailable` / `db_unavailable` | 503 | dependency down |
| `idempotency_conflict` | 409 | conflicting idempotency key |
| `server_config_error` | 500 | server misconfiguration |
| `internal_error` | 500 | unexpected |

**Example error responses per status:**

```json
// 400 validation_error
{"success": false, "error": {"code": "validation_error", "message": "'abc' is not a valid phone number for whatsapp.", "field": "channels[0].contact"}}

// 401 unauthorized
{"success": false, "error": {"code": "unauthorized", "message": "Invalid or missing API key. Send X-API-Key header.", "field": null}}

// 403 forbidden
{"success": false, "error": {"code": "forbidden", "message": "API key lacks scope 'send:whatsapp'.", "field": null}}

// 404 not_found
{"success": false, "error": {"code": "not_found", "message": "No notification found with id 'abc-123'.", "field": null}}

// 409 idempotency_conflict
{"success": false, "error": {"code": "idempotency_conflict", "message": "Idempotency-Key already used with a different payload.", "field": null}}

// 422 unprocessable_entity (Pydantic detail)
{"success": false, "error": {"code": "unprocessable_entity", "message": "1 validation error for SendRequest", "field": "channels"}}

// 429 rate_limited (Retry-After: 37)
{"success": false, "error": {"code": "rate_limited", "message": "Send limit exceeded for this key/recipient/channel.", "field": null}}

// 500 internal_error
{"success": false, "error": {"code": "internal_error", "message": "Internal server error.", "field": null}}
```

## 2.6 Idempotency Contract

- Client sends optional `Idempotency-Key` header.
- API checks Redis cache: if a notification with the same key exists, return the original `202` (with `X-Idempotent-Replay: true`) instead of enqueuing a duplicate.
- The key is also persisted in PostgreSQL (`idempotency_keys` table) for durable dedup.
- Conflicting payloads with the same key → `409 idempotency_conflict`.
- The key TTL is at least `DELIVERY_TIMEOUT_SECONDS` + provider max retry horizon (default 24 h).

## 2.7 Rate Limiting Contract

See [08-RATE-LIMITING.md](08-RATE-LIMITING.md).

- Per-API-key send limit (default e.g. 100 req/min).
- Per-recipient limit (e.g. 20 sends/hour) to prevent abuse.
- Per-channel provider limit mapped to provider quotas.
- On exceed: `429 rate_limited` with `Retry-After` header; the message is not enqueued.