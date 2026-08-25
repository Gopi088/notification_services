# 28 — Error Handling, External Templates & Message Routing

## 28.1 Overview

This document defines how the platform handles:

1. **Errors** — typed, structured, consistent across API / providers / queue / workers.
2. **External templates** — per-channel template support (SMS, WhatsApp, Email) so one
   logical message can be rendered differently per channel.
3. **Inbound replies** — what happens when a recipient replies to a notification.
4. **Message routing** — how a message is formatted for each channel.

## 28.2 Error Handling

### 28.2.1 Philosophy

- Every layer raises a **typed exception**.
- The API layer maps each exception to an HTTP status and a **uniform envelope**:
  ```json
  {"success": false, "error": {"code": "...", "message": "...", "field": "..."}}
  ```
- Provider exceptions are classified (retryable vs permanent) — see
  [07-RETRY-IDEMPOTENCY.md](07-RETRY-IDEMPOTENCY.md).
- Secrets are never included in error messages.

### 28.2.2 Error Types (`app/errors.py`)

| Class | HTTP | code |
| ----- | ---- | ---- |
| `AppError` (base) | 500 | `internal_error` |
| `ValidationError` | 400 | `validation_error` |
| `UnauthorizedError` | 401 | `unauthorized` |
| `ForbiddenError` | 403 | `forbidden` |
| `NotFoundError` | 404 | `not_found` |
| `IdempotencyConflictError` | 409 | `idempotency_conflict` |
| `UnprocessableError` | 422 | `unprocessable_entity` |
| `RateLimitError` | 429 | `rate_limited` |
| `ProviderUnavailableError` | 502 | `provider_unavailable` |
| `QueueUnavailableError` | 503 | `queue_unavailable` |
| `DatabaseUnavailableError` | 503 | `db_unavailable` |
| `ConfigurationError` | 500 | `server_config_error` |

### 28.2.3 Mapping

`app/main.py::unhandled_exception_handler`:

- `AppError` → its own status + envelope.
- `ProviderError(retryable=True)` → `502 provider_unavailable`.
- `ProviderError(retryable=False)` → `400 validation_error`.
- `ProviderConfigError` → `500 server_config_error`.
- anything else → `500 internal_error` (no details leaked).

### 28.2.4 Provider Error Classification

`app/providers/base.py`:

- `ProviderError(message, retryable=bool, error_code=str)` — base.
- `ProviderConfigError` — missing credentials (never retryable).

| Provider condition | retryable | code |
| ------------------ | --------- | ---- |
| timeout / network | true | `NETWORK` |
| HTTP 429 | true | `429` |
| HTTP 5xx | true | `500` |
| HTTP 401/403/404/422 | false | status string |
| invalid response / missing id | false | `BAD_RESPONSE` / `NO_MESSAGE_ID` |

## 28.3 External Templates (per channel)

One logical message can be rendered differently per channel through **external
templates**. The core orchestrator stays channel-agnostic; each provider renders
through its own template mechanism.

### 28.3.1 SMS Templates

Plain-text templates in `templates/sms/<name>.txt` with `{{var}}` placeholders:

```
templates/sms/otp.txt:
    Your OTP is {{body}}. Valid for 10 minutes.

templates/sms/greeting.txt:
    Hi {{name}}, your {{body}}.
```

- `{{body}}` = the original message.
- `{{name}}` = a `template_params` value.
- Missing template file → falls back to the plain message (SMS never breaks).

Usage via `app/message_format.format_sms()` and `send_with_template(...)`.

### 28.3.2 WhatsApp Templates

Meta-approved templates referenced by name (WhatsApp Business Manager):

```
{
  "channel": "whatsapp",
  "contact": "+919887270348",
  "template_name": "interview_confirmation",
  "template_language": "en",
  "template_params": [{"name": "name", "value": "Rahul"}]
}
```

- Providers: Azure `send_template` (MessageTemplate + bindings), Vonage passthrough.
- Free-form text only works inside a 24h session window; templates reach new contacts.

### 28.3.3 Email Templates

HTML templates in `templates/email/<name>.html` with `{{subject}}`/`{{body}}`:

```
templates/email/welcome.html:
    <h1>{{subject}}</h1><p>{{body}}</p>
```

- `subject` may be overridden via `template_params: {"subject": "..."}`.
- Missing template → falls back to escaped plain HTML.

### 28.3.4 Message Format Routing (`app/message_format.py`)

`format_for_channel(channel, message, template_name, template_language, template_params)`:

| channel | returns |
| ------- | ------- |
| `sms` | plain string (or template-rendered string) |
| `whatsapp` | `{"text": message}` or `{"template": name, "language": ..., "params": {...}}` |
| `email` | `{"subject": ..., "html": ...}` |
| other | raises `MessageFormatError` |

This lets the caller send one message and have it routed into the correct shape
for every channel automatically.

## 28.4 Inbound Replies (recipient responses)

### 28.4.1 Endpoint

```
POST /api/v1/inbound
```

Providers (Vonage, Azure) POST inbound events here. The endpoint is
provider-agnostic and accepts a normalized shape:

```json
{
  "channel": "whatsapp",
  "from": "+919887270348",
  "to": "+1484xxxxxxx",
  "text": "Yes, I confirm my interview",
  "message_uuid": "inbound-1"
}
```

Azure-style (`channelType`, `messageId`, `message` inside `data`) is also
normalized.

### 28.4.2 What happens

1. The inbound message is persisted in the `inbound_messages` table.
2. An audit record `notification_received` is written (who replied, which
   channel, result).
3. Optionally an **auto-reply** is sent back (2-way conversation).

### 28.4.3 Auto-reply

Enable with `.env`:

```
INBOUND_AUTO_REPLY=true
INBOUND_AUTO_REPLY_TEXT=Thanks for your message! We'll be in touch soon.
```

When enabled, each inbound reply triggers an outbound send back to the same
number through the same channel.

### 28.4.4 Data model

`inbound_messages` table:

| column | type |
| ------ | ---- |
| id | PK |
| channel | TEXT |
| from_number | TEXT |
| to_number | TEXT |
| text | TEXT |
| provider_message_id | TEXT |
| raw | JSONB |
| created_at | TIMESTAMPTZ |

`GET /api/v1/inbound` is the provider validation challenge (returns ok).

## 28.5 Configuration

| Env | Default | Purpose |
| --- | ------- | ------- |
| `INBOUND_AUTO_REPLY` | `false` | enable auto-reply on inbound |
| `INBOUND_AUTO_REPLY_TEXT` | `""` | auto-reply body |
| `TEMPLATES_DIR` | `templates` | base templates dir |

## 28.6 Tests

See `tests/test_format_errors_inbound.py` covering:

- `format_sms` / `format_sms_template` (with and without params, missing-file fallback)
- `format_whatsapp` text vs template
- `format_email` plain vs template, missing-template error
- `format_for_channel` per channel + unknown channel
- `AppError` types, `classify_provider_error`
- inbound webhook (normalized, Azure-style, nested data, empty text, auto-reply)
- inbound persistence + audit
- SMS template via provider `send_with_template`
- main exception handler (NotFound → 404, ProviderError → 502, ConfigError → 500)
