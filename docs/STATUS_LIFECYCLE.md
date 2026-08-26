# Status Lifecycle

## 7.1 Purpose

Defines the status lifecycle and the immediate status API contract. A client
may call `GET /status` immediately after `POST` — before the provider has
finished processing.

## 7.2 State Machine

```
created
  → queued
  → processing
  → submitted
  → delivered
  → read        (provider supports it, e.g. WhatsApp)
  → acknowledged (explicit user response)
  → failed
  → retrying
  → dead_lettered
  → cancelled
  → scheduled
  → expired
```

See [NOTIFICATION_LIFECYCLE.md](NOTIFICATION_LIFECYCLE.md) for the full
transition table.

## 7.3 Immediate Status Request

```
Client sends notification
        ↓
API returns notification_id / group_id
        ↓
Client immediately calls GET /status
        ↓
Notification may still be queued/processing
```

Rules:

1. `GET /status` **never assumes delivery** — it reads the latest persisted
   state.
2. It is fast and **never waits for the provider**.
3. If the notification is still `queued`/`processing`, that is returned as-is.

### Status response shape

```json
{
  "success": true,
  "message_id": "group-uuid",
  "reference": "ORDER-123",
  "status": "processing",
  "channels": [
    {
      "message_id": "msg-uuid",
      "channel": "whatsapp",
      "contact": "+919887270348",
      "status": "processing",
      "provider": "vonage_whatsapp",
      "provider_message_id": "abc-123",
      "error": null,
      "created_at": "...",
      "updated_at": "...",
      "elapsed_seconds": 1.2,
      "timed_out": false,
      "delivery_timeout_seconds": 300,
      "read_at": null,
      "acknowledged_at": null,
      "acknowledgement_type": null
    }
  ]
}
```

## 7.4 Lifecycle by Example

```
POST /api/v1/notifications/send
  → 202 { status: queued }

GET  /api/v1/notifications/{id}/status
  → status: queued          (worker not yet claimed)

(worker claims)
  → status: processing

(provider accepts)
  → status: submitted   (provider_message_id set)

(provider webhook delivered)
  → status: delivered

(provider webhook read, WhatsApp)
  → status: read

(user replies "YES")
  → status: acknowledged
```

## 7.5 Delivered ≠ Read ≠ Acknowledged

| Term | Meaning |
| ---- | ------- |
| Delivered | Provider confirmed the message reached the device. |
| Read | Provider confirmed the recipient opened/read it (WhatsApp only). |
| Acknowledged | Application received an explicit user response/action. |

- SMS never transitions to `read` (providers don't emit it).
- WhatsApp supports `read` when the provider sends the event.
- Email open/click is provider-dependent and best-effort.

## 7.6 Audit on Every Transition

Each transition writes an audit record: `notification_created`,
`notification_queued`, `notification_processing`, `notification_submitted`,
`notification_delivered`, `notification_failed`, `notification_read`,
`notification_acknowledged`, `notification_expired`, `retry_scheduled`,
`retry_attempted`, `retry_exhausted`, `queue_failure`, `worker_failure`.

See [NOTIFICATION_LIFECYCLE.md](NOTIFICATION_LIFECYCLE.md).
