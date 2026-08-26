# Status & Acknowledgement

## 2.1 Purpose

Defines the status API contract (including immediate `GET /status` after
`POST`) and the distinction between **delivered**, **read**, and
**acknowledged**. Explains how provider delivery/read events and user replies
are processed, stored, and surfaced.

## 2.2 Immediate Status Request

Scenario:

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

1. `GET /status` **never assumes delivery**. It reads the latest persisted
   state from the database and returns it.
2. `GET /status` is fast and **never waits for the provider**.
3. If the notification is still `queued`/`processing`, that is returned as-is.

### Endpoints

- `POST /api/v1/notifications/send` → `202` with `notification_id` (group id),
  `reference`, `status`, and per-channel `message_id`s.
- `GET /api/v1/notifications/{notification_id}/status` → status of a single
  message or a group (backward compatible).
- `GET /api/v1/notifications/group/{group_id}/status` → status of a whole
  group (same response shape).

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
      "created_at": "2026-08-25T10:00:00Z",
      "updated_at": "2026-08-25T10:00:01Z",
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

## 2.3 Delivered ≠ Read ≠ Acknowledged

| Term | Definition | Example |
| ---- | ---------- | ------- |
| **Delivered** | Provider confirmed the message reached the recipient's device. | WhatsApp: Meta `delivered` webhook. |
| **Read** | Provider confirmed the recipient opened/read it — only when the provider supports it. | WhatsApp: `read` webhook. |
| **Acknowledged** | The application received an explicit user response or performed an acknowledgement action. | User replies "YES" via SMS/WhatsApp inbound webhook. |

- **SMS**: delivery confirmation from the provider does **not** imply the user
  read or acknowledged the message. SMS never transitions to `read`.
- **WhatsApp**: support provider `delivered` and `read` events when the
  provider sends them.
- **Email**: distinguish provider delivery / open / click where available;
  document that open/click tracking depends on the provider and is best-effort.

## 2.4 Provider Status Storage

Webhook events are stored durably and idempotently.

| Column | Purpose |
| ------ | ------- |
| `provider_message_id` | provider-side message id (join key) |
| `provider_status` | raw provider status string |
| `normalized_status` | mapped internal state |
| `event_timestamp` | provider-reported time |
| `received_timestamp` | time we received the webhook |
| `channel` | whatsapp / sms / email |
| `notification_id` | our internal id (if resolved) |
| `raw` | redacted raw event metadata (never secrets) |

Storage: `webhook_events` table (append-only). Lookup by
`provider_message_id`; webhook processing is **idempotent** (repeated events
for the same provider_message_id are no-ops once terminal).

## 2.5 User Acknowledgement

- `acknowledged_at` — when the acknowledgement was recorded.
- `acknowledgement_type` — e.g. `reply`, `button`, `api`.
- `acknowledgement_message` / `acknowledgement_reference` — the response text
  or a reference id.
- `acknowledgement_source` — `inbound_sms`, `inbound_whatsapp`, `api`, ...

Acknowledgement is an **explicit user action** — it is never inferred from
delivery or read events.

## 2.6 Implementation

- `app/schemas.py` — extend `ChannelStatus` with `read_at`,
  `acknowledged_at`, `acknowledgement_type`.
- `app/storage.py` — add `read_at`, `acknowledged_at`,
  `acknowledgement_type`, `acknowledgement_message`, `acknowledgement_source`
  columns; add `mark_read()` and `mark_acknowledged()` helpers that apply the
  guarded transitions `delivered→read` / `delivered|read→acknowledged`.
- Webhook handler maps provider `read` → `read` and stores event rows.
- Inbound handler records user replies as acknowledgements.
- `app/orchestrator.py::get_message_summary` / `get_group_summary` surface the
  new fields.
