# Notification Lifecycle

## 1.1 Purpose

Defines the end-to-end lifecycle of a notification — every state a notification
can be in, the legal transitions between states, and how provider events map
into our internal normalized states. This is the source of truth for status
responses, worker logic, webhook processing, acknowledgement, and audit.

## 1.2 Normalized State Machine

We expose a single, stable set of states to clients. Provider-specific statuses
(WhatsApp `read`, Vonage `delivered`, Azure `Failed`, ...) are **mapped** into
these normalized states — never exposed raw.

```
                   ┌────────────┐
                   │  created   │
                   └─────┬──────┘
                         │ persisted + enqueued
                         ▼
                   ┌────────────┐
         ┌────────▶│   queued   │◀──────────────┐
         │         └─────┬──────┘               │
         │               │ worker claims        │ retry (attempts < max)
         │               ▼                       │
         │         ┌──────────────┐              │
         │   ┌────▶│  processing  │──────────────┘
         │   │     └──────┬───────┘
         │   │            │ provider accepts
         │   │            ▼
         │   │      ┌─────────────┐
         │   │      │  submitted  │  (has provider_message_id)
         │   │      └──────┬──────┘
         │   │             │ provider webhook: delivered / failed / read
         │   │             ▼
         │   │   ┌─────────────┐   ┌───────────────┐
         │   │   │  delivered  │   │    failed      │
         │   │   └──────┬──────┘   └───────┬───────┘
         │   │          │                  │ non-retryable / exhausted
         │   │          │                  ▼
         │   │          │           ┌──────────────┐
         │   │          │           │ dead_lettered│
         │   │          │           └──────────────┘
         │   │          │
         │   │          ▼  (optional, provider supports read)
         │   │     ┌──────────┐
         │   └────▶│   read    │
         │         └──────────┘
         │
         └──────────── (cancelled / scheduled / acknowledged / expired)
```

### Normalized states

| State | Meaning |
| ----- | ------- |
| `created` | Request validated; notification row about to be persisted. |
| `queued` | Persisted; waiting for a worker to pick it up (or an in-process queue). |
| `processing` | A worker claimed it and is calling the provider. |
| `submitted` | Provider accepted the message (we have `provider_message_id`). |
| `delivered` | Provider webhook confirmed delivery to the recipient's device. |
| `failed` | Permanent failure (non-retryable) or retries exhausted. |
| `retrying` | Transient failure; a retry is scheduled (exponential backoff). |
| `dead_lettered` | Retries exhausted; moved to the dead-letter queue. |
| `cancelled` | Manually cancelled before delivery. |
| `scheduled` | `send_at` is in the future or outside the allowed send window. |
| `read` | Provider confirms the recipient opened/read it (WhatsApp only). |
| `acknowledged` | Application received an explicit user response/action. |
| `expired` | A scheduled/out-of-hours notification whose send window passed. |

## 1.3 Legal Transitions

Only the transitions below are allowed. Any other transition is **rejected**
(logged + audited as an invalid transition; the current state is preserved).

| From | To |
| ---- | -- |
| `created` | `queued`, `scheduled`, `cancelled`, `failed` |
| `queued` | `processing`, `cancelled`, `expired` |
| `processing` | `submitted`, `failed`, `retrying`, `delivered`, `cancelled` |
| `submitted` | `delivered`, `failed`, `read`, `expired` |
| `delivered` | `read`, `acknowledged` |
| `read` | `acknowledged` |
| `retrying` | `processing`, `cancelled` |
| `failed` | `retrying`, `dead_lettered` |
| `dead_lettered` | `retrying` (manual requeue) |
| `scheduled` | `queued`, `cancelled`, `expired` |
| `expired` | `cancelled` |
| `acknowledged` | *(terminal)* |
| `cancelled` | *(terminal)* |

Terminal states: `delivered`, `failed`, `dead_lettered`, `cancelled`,
`acknowledged`, `expired`. Terminal states accept no further transitions except
`acknowledged` from `delivered`/`read`.

## 1.4 Provider → Normalized Mapping

| Provider event | Normalized | Notes |
| -------------- | ---------- | ----- |
| Vonage `delivered` | `delivered` | |
| Azure `delivered` | `delivered` | |
| Azure `read` (WhatsApp) | `read` | only when supported |
| Vonage `read` (WhatsApp) | `read` | only when supported |
| Azure `failed` | `failed` | attach error code/message |
| Vonage `failed` | `failed` | attach error code/message |
| Provider `accepted` (sync response) | `submitted` | we hold `provider_message_id` |
| Provider `expired` | `expired` | e.g. 24h session window elapsed |

Only map **actual** provider events; never invent states the provider does not
emit. SMS providers do not emit read events — SMS never transitions to `read`.

## 1.5 Request Lifecycle (timeline)

```
Client
  │ POST /api/v1/notifications/send
  ▼
validate → idempotency → rate-limit → persist (created→queued) → enqueue
  │
  ▼ 202 Accepted {notification_id / group_id, status: queued}
Worker
  │ consume → queued→processing
  ▼
provider.send → submitted (provider_message_id)
  │
  ▼
Provider webhook → delivered / failed / read
  │
  ▼
Client GET /status → latest persisted state (never assumed)
```

## 1.6 Implementation

- State constants + `TRANSITIONS` guard live in `app/storage.py`.
- `Storage.transition()` enforces the guard and writes a
  `notification_events` row + audit record on every change.
- The worker and webhook handlers call `transition()`; invalid transitions are
  logged + audited and leave state unchanged.
