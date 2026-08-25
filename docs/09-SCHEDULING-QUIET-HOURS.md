# 09 — Scheduling & Quiet Hours

## 9.1 Overview

Notifications may be sent immediately (`send_at` empty) or scheduled to a
future time (`send_at`). Quiet hours / allowed send windows defer notifications
that would otherwise send at an inappropriate time (e.g. 23:30).

Behavior policy (consistent across all docs):

- A `send_at` in the **past** is sent immediately.
- A `send_at` in the **future** schedules the notification (status `scheduled`).
- A request made **during quiet hours** (outside the allowed window) is
  **deferred** to the next allowed time — it is **never silently rejected**.
- A request made **during the allowed window** sends immediately.

## 9.2 Data Model

`notifications` table already carries:

- `scheduled_at TIMESTAMPTZ` — the exact scheduled send time (UTC).
- `timezone TEXT` — user/request timezone (e.g. `Asia/Kolkata`) used to
  interpret `send_at` local time.

## 9.3 Request Format

```json
{
  "channels": [{"channel": "sms", "contact": "+919887270348"}],
  "message": "Your interview is confirmed.",
  "send_at": "2026-08-26T10:30:00+05:30",
  "timezone": "Asia/Kolkata"
}
```

- `send_at` is optional; when omitted the notification sends immediately
  (subject to quiet hours).
- `send_at` accepts an offset (`+05:30`) or a naive local time interpreted
  with `timezone` (default `UTC`).

## 9.4 Normalization

1. Parse `send_at`; if it has no offset, apply `timezone` via `zoneinfo`.
2. Convert to UTC and store in `scheduled_at` (UTC).
3. If `scheduled_at <= now` → status `queued` (send immediately).
4. Else → status `scheduled`, `next_attempt_at = scheduled_at`.

## 9.5 Flow

```
API
 ↓ validate send_at + timezone
 ↓ normalize to UTC
 ↓ store notification (status = queued | scheduled)
 ↓
if scheduled: wait until next_attempt_at
 ↓
queue
 ↓
worker
 ↓
provider
```

## 9.6 Allowed Send Windows (Quiet Hours)

Configurable per channel:

| Channel | Allowed window (default) | Env vars |
| ------- | ------------------------ | -------- |
| SMS | 09:00 – 21:00 | `QUIET_HOURS_SMS_START=09:00`, `QUIET_HOURS_SMS_END=21:00` |
| WhatsApp | 09:00 – 21:00 | `QUIET_HOURS_WHATSAPP_START`, `QUIET_HOURS_WHATSAPP_END` |
| Email | 08:00 – 22:00 | `QUIET_HOURS_EMAIL_START`, `QUIET_HOURS_EMAIL_END` |

Windows are interpreted in the notification's `timezone`. Empty config disables
quiet hours for that channel.

## 9.7 Deferral Policy

If the computed send time falls **outside** the allowed window:

- Compute the next allowed send time (next day's window start).
- Set `scheduled_at = next allowed time`, status `scheduled`.
- Audit `notification_deferred` with the deferral reason.
- Log `quiet hours active`, `next allowed send time`.

## 9.8 Timezone Handling

- All timestamps stored in **UTC** (`TIMESTAMPTZ`).
- `timezone` stored per request; default `UTC`.
- Conversion via `zoneinfo.ZoneInfo(timezone)`; invalid timezone → 422
  `validation_error`.
- DST: use IANA timezone data so `Asia/Kolkata` (no DST) and
  `America/New_York` (DST) resolve correctly.
- Past `send_at` → immediate send.

## 9.9 Edge-Case Policy

| Case | Behavior |
| ---- | -------- |
| Exactly at window start | Allowed (inclusive start) |
| Exactly at window end | Allowed (inclusive end) |
| 1 minute before start | Deferred to next start |
| 1 minute after end | Deferred to next start |
| Midnight (00:00) | Outside window → deferred |
| Timezone change | Recompute in request timezone at schedule time |
| DST transition | `zoneinfo` handles; gap/overlap resolved by IANA rules |
| Weekend | Not special-cased (windows apply every day) |
| Holiday | Not built-in; configurable later |
| `send_at` in past | Send immediately |
| Invalid timezone | 422 |
| Invalid `send_at` format | 422 |
| Already queued before quiet hours begin | If the worker reaches it inside quiet hours, defer once to next window |
| Processing when quiet hours begin | In-flight send completes; no re-deferral mid-send |

## 9.10 Worker Integration

The worker checks `scheduled_at`/`next_attempt_at` before sending:

- If `scheduled_at > now` → defer (requeue with remaining delay).
- If quiet hours active and the channel has a window → defer to next window.

These checks run in the worker (or a dedicated scheduler) so the API never
blocks on timing.

## 9.11 Scheduled Notifications Status

Status values used:

- `scheduled` — future send time set; not yet due.
- `queued` — due and enqueued for the worker.
- `processing` — worker picked it up.

The user can query `GET /notifications/{id}` and see `scheduled` with the
`send_at`/`scheduled_at` time.

## 9.12 Tests

See [16-EDGE-CASES.md](16-EDGE-CASES.md) and the evals in
[`evals/scheduling.yaml`](evals/scheduling.yaml).
