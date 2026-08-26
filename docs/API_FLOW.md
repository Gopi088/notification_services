# API Flow

## 8.1 Overview

Two main APIs, with a fully asynchronous send flow.

- `POST /api/v1/notifications/send` — accept + persist + queue, return 202.
- `GET /api/v1/notifications/{notification_id}/status` — return latest persisted state immediately.

## 8.2 Send Flow

```
Client
  ↓ POST /api/v1/notifications/send
Authentication (X-API-Key)
  ↓
Validation (schema + contact)
  ↓
Rate limiting (Redis)
  ↓
Idempotency claim (DB unique key)
  ↓
Persist notification (status=queued)
  ↓
Publish job to queue (Redis Streams / memory)
  ↓
Return HTTP 202 { notification_id, group_id, request_id, status: queued }
  ↓
Worker consumes job
  ↓
status → processing
  ↓
Provider send
  ↓
status → submitted (provider_message_id)
  ↓
Provider webhook → delivered / failed / read / acknowledged
```

The API **never waits** for the provider.

## 8.3 Send Response

```json
{
  "success": true,
  "message_id": "group-uuid",
  "reference": "ORDER-123",
  "status": "queued",
  "channels": [
    {"message_id": "msg-uuid", "channel": "whatsapp", "status": "queued", "contact": "+919887270348"}
  ]
}
```

## 8.4 Status Flow

```
GET /api/v1/notifications/{notification_id}/status
  → queued   (worker not yet claimed)
  → processing (worker claimed)
  → submitted  (provider accepted)
  → delivered  (provider webhook)
  → read       (provider read event, WhatsApp)
  → acknowledged (explicit user response)
  → failed     (permanent failure)
  → retrying   (transient failure, backoff scheduled)
```

The status endpoint reads the latest persisted state and **never waits for the
provider**.

## 8.5 Response Headers

- `X-Request-ID` — echoed / generated correlation id.
- `X-Idempotent-Replay: true` — response is a replay of an earlier identical
  request with the same Idempotency-Key.

## 8.6 Errors

All errors use a uniform envelope:

```json
{"success": false, "error": {"code": "...", "message": "...", "field": "..."}}
```

Common codes: `validation_error` (400), `unauthorized` (401), `forbidden`
(403), `not_found` (404), `idempotency_conflict` (409), `unprocessable_entity`
(422), `rate_limited` (429), `provider_unavailable` (502),
`queue_unavailable`/`db_unavailable` (503), `internal_error` (500).
