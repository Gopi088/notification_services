# Notification Service

A versioned REST API for sending notifications over **WhatsApp**, **SMS**, and
**Email**, and tracking their delivery status. No UI — everything is driven
from the terminal with `curl` (or any HTTP client).

- **Versioned API**: `/api/v1` — breaking changes go to `/api/v2` while v1 keeps working.
- **Channel isolation**: each medium (whatsapp/sms/email) has its own provider; one request can fan out to several channels at once.
- **External templates**: per-channel templates control how a message is rendered (required by WhatsApp for first-time contacts).
- **Uniform response envelope**: `{success, ..., error}` on every endpoint.

## Project structure

```
notification-service/
├── app/
│   ├── main.py                     # FastAPI app, startup, global error handler
│   ├── config.py                   # Settings loaded from .env
│   ├── database.py                 # SQLite message store (queued/sent/delivered/failed)
│   ├── schemas.py                  # Versioned Pydantic request/response models
│   ├── validation.py               # Contact (phone/email) format validation
│   ├── orchestrator.py             # Routes sends to channel providers, groups status
│   ├── providers/
│   │   ├── base.py                 # NotificationProvider interface + exceptions
│   │   ├── azure_provider.py       # Azure ACS: WhatsApp + SMS + Email
│   │   └── factory.py              # channel -> provider lookup (extend here for new channels)
│   └── routers/
│       ├── v1.py                   # Versioned API: /api/v1/...
│       └── notifications.py        # Legacy routes (backward compatible)
├── .env.example                    # Copy to .env and fill in credentials
├── examples/
│   └── event.example.json          # Ready-to-send event payload (whatsapp+sms+email)
├── requirements.txt
├── run.sh
└── README.md
```

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env:
# - Azure connection string -> WhatsApp, SMS and Email (one key)

# Leave MOCK_MODE=true to test the whole flow with zero credentials -
# every request below works in mock mode.

# 4. Run the API
./run.sh
# Equivalent: venv/bin/uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port 8000
```

> **WSL / Windows-drive note:** if you run this from a `/mnt/c/...` path (a
> 9p mount), `uvicorn --reload` can crash with
> `_rust_notify.WatchfilesRustInternalError: Cannot allocate memory (os error 12)`.
> `run.sh` handles this by watching only `app/` and forcing a polling file
> watcher (`WATCHFILES_FORCE_POLLING=true`) on WSL. Do not run the bare
> `uvicorn ... --reload` command shown in older versions of this file from
> `/mnt/c`.

The API is now at `http://127.0.0.1:8000`. Interactive docs (Swagger UI) are
available at `http://127.0.0.1:8000/docs` if you want to explore it in a
browser, but every operation below works purely from the command line.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `MOCK_MODE` | `true` = simulate sends, no credentials needed (default). `false` = use real providers below. |
| `DATABASE_PATH` | SQLite file used to track message status. |
| `AUTH_ENABLED` | `true` = require `X-API-Key` header on every `/api/v1` request. |
| `AUTH_API_KEY` | The API key clients must send when `AUTH_ENABLED=true`. |
| `TEMPLATES_DIR` | Directory holding channel templates (email HTML files). |
| `EMAIL_TEMPLATE_NAME` | Default email template used when a request omits `template_name`. |
| `AZURE_COMMUNICATION_CONNECTION_STRING` | **One connection string covers all 3 channels.** Azure portal → Communication Services → your resource → Keys. |
| `AZURE_DEFAULT_COUNTRY_CODE` | Added to phone numbers that have no country code (default `91`). |
| `AZURE_SMS_FROM` | Your SMS-enabled ACS phone number (E.164), e.g. `+919812345678`. |
| `AZURE_EMAIL_FROM` | Sender verified in Azure Email Communication Service, e.g. `DoNotReply@yourdomain.com`. |
| `AZURE_WHATSAPP_CHANNEL_ID` | WhatsApp channel registration ID (Azure portal → Advanced Messaging). |

## Authentication

Set `AUTH_ENABLED=true` and `AUTH_API_KEY=<your-secret>` in `.env`. Every
`/api/v1` request must then include the API key:

```bash
curl -H "X-API-Key: <your-secret>" http://127.0.0.1:8000/api/v1/health
```

Wrong or missing keys get `401` with `{"success": false, "error": {"code": "unauthorized", ...}}`.
The CLI uses the same `AUTH_API_KEY` setting (single source of truth):

```bash
# AUTH_API_KEY is read from .env by both the server and CLI
./cli.sh send email you@example.com "Hello"
```

## Email templates

Email messages render through HTML templates in `templates/email/<name>.html`.
Templates may use `{{subject}}` and `{{body}}` placeholders (values are
HTML-escaped automatically). A `default` template ships with the project.

Create your own, e.g. `templates/email/welcome.html`:

```html
<h1>{{subject}}</h1>
<p>Hi, {{body}}</p>
```

Select it per channel with `template_name`:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/notifications/send \
  -H "Content-Type: application/json" \
  -d '{
        "channels": [
          {"channel": "email", "contact": "you@example.com", "template_name": "welcome"}
        ],
        "message": "Thanks for signing up"
      }'
```

The interactive CLI asks for a template name, and one-shot commands accept
`--template <name>`:

```bash
./cli.sh send email you@example.com "Hello" --template welcome
python3 notification_service.py send email you@example.com "Hello" --template welcome
```

All three channels (WhatsApp, SMS, Email) run on **Azure Communication
Services** with a single connection string. WhatsApp needs a connected
WhatsApp Business account + Meta-approved template for first-time contacts;
free text only works inside a 24h session window (a WhatsApp rule, not Azure).

## API

### Command-line interface (recommended)

Start the server once (`./run.sh`), then use `./cli.sh` from a second
terminal exactly like any Linux command:

```bash
# Interactive mode - it asks you for every parameter, no typing needed
./cli.sh

# One-shot commands
./cli.sh send whatsapp 919812345678 "Hello from the notification service"
./cli.sh send sms     919812345678 "Your OTP is 482913"
./cli.sh send email    someone@example.com "Your order has shipped"
./cli.sh send-event   examples/event.example.json   # send your event payload
./cli.sh status 147fc2d8-5fa9-4ec4-a64a-94b1eff15518

# Verbose - show the full API response (disable with -v omitted)
./cli.sh -v status 147fc2d8-5fa9-4ec4-a64a-94b1eff15518
```

The server auto-starts if it isn't already running, so you can just run
`./cli.sh` on its own.

**Python CLI (same thing, Python style):**

```bash
python3 notification_service.py                       # interactive menu
python3 notification_service.py send sms 9887270348 "Hello"
python3 notification_service.py status <message_id>
```

`send` replies with the `message_id` immediately (status `queued`), then the
message is delivered in the background. `status` reports whether it was
`DELIVERED` or `FAILED` (with the reason), plus which provider handled it.

## Versioned API (`/api/v1`)

All endpoints live under `/api/v1` and return a uniform envelope:

- **Success**: `{"success": true, ...}`
- **Error**: `{"success": false, "error": {"code": "...", "message": "...", "field": null}}`

### `GET /api/v1/health`

```bash
curl http://127.0.0.1:8000/api/v1/health
```

```json
{"success": true, "service": "Notification Service", "version": "2.0.0", "mock_mode": false}
```

### `POST /api/v1/notifications/send`

Queues one message across one or more channels. Each channel is routed only to
its own provider — sms can never reach the whatsapp provider or vice-versa.
Optionally attach an external template per channel.

**Request body**

| Field | Type | Notes |
|---|---|---|
| `channels[]` | array | 1+ channel objects |
| `channels[].channel` | string | `whatsapp`, `sms`, or `email` |
| `channels[].contact` | string | phone (E.164-ish) for whatsapp/sms, email for email |
| `channels[].template_name` | string? | external template (required by WhatsApp for new contacts) |
| `channels[].template_language` | string? | e.g. `en` / `en_US` (default from config) |
| `channels[].template_params[]` | array? | `{name, value}` substitutions |
| `message` | string | 1–4096 characters, shared across channels |
| `reference` | string? | caller id, e.g. an order id (returned in status) |

**cURL — send over email + WhatsApp in one call**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/notifications/send \
  -H "Content-Type: application/json" \
  -d '{
        "channels": [
          {"channel": "email", "contact": "someone@example.com"},
          {"channel": "whatsapp", "contact": "+919887270348"}
        ],
        "message": "Your order has shipped",
        "reference": "ORD-1234"
      }'
```

**Response — `202 Accepted`**

```json
{
  "success": true,
  "message_id": "593d453e-928d-4b69-934b-51d8b80cc200",
  "reference": "ORD-1234",
  "status": "queued",
  "channels": [
    {"message_id": "ca3bb7c6-...", "channel": "email", "status": "queued", "contact": "someone@example.com"},
    {"message_id": "1d204523-...", "channel": "whatsapp", "status": "queued", "contact": "+919887270348"}
  ]
}
```

**With a WhatsApp template (to reach new numbers)**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/notifications/send \
  -H "Content-Type: application/json" \
  -d '{
        "channels": [
          {
            "channel": "whatsapp",
            "contact": "+919887270348",
            "template_name": "test_template",
            "template_language": "en",
            "template_params": [{"name": "body", "value": "Hi, your order is ready"}]
          }
        ],
        "message": "Hi, your order is ready"
      }'
```

**Validation error — `400`** (bad contact, duplicate channel, etc.)

```json
{
  "success": false,
  "error": {
    "code": "validation_error",
    "message": "'abc' is not a valid phone number for whatsapp. Use E.164 format, e.g. +14155551234.",
    "field": "channels"
  }
}
```

### `POST /api/v1/notifications/event`

Event-driven send. One envelope (`request_id` / `event_type` / `ref` / `data`)
with a `deliveries` list — each delivery targets exactly one channel with its
own recipient and channel-specific payload, so any message can be sent to
anyone over WhatsApp, SMS and Email in a single call.

**Request body**

| Field | Type | Notes |
|---|---|---|
| `request_id` | string? | caller id for the request |
| `event_type` | string? | what triggered the send, e.g. `interview_confirmation` |
| `ref` | string? | caller reference (returned in status, used as `reference`) |
| `data` | any? | event data; a string is used as a fallback message body, a dict as fallback WhatsApp template params |
| `deliveries[]` | array | 1+ delivery objects |
| `deliveries[].channel` | string | `whatsapp`, `sms`, or `email` |
| `deliveries[].payload` | object | shape depends on the channel (below) |

**Per-channel payloads**

- **whatsapp**: `recipient` (E.164 phone), optional `message` (free text, only
  inside a 24h session window), optional `template` with `id` (approved Meta
  template name — required to reach a new contact), `language` and optional
  `params` (`[{name, value}]`).
- **sms**: `recipient` + `message`.
- **email**: `recipient`, optional `subject`, `message` (plain text), `html`
  (HTML body), `cc`, `bcc`, `replyTo`, and `attachments`
  (`[{name, url|content_base64, type}]` — attachments are downloaded from
  `url` and base64-encoded before sending).

**cURL — send WhatsApp + SMS + Email in one call**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/notifications/event \
  -H "Content-Type: application/json" \
  -d @examples/event.example.json
```

The response is identical to `/notifications/send`: `202 Accepted` with a
group `message_id` and one queued entry per delivery.

**CLI one-liner** (server auto-starts if needed):

```bash
./cli.sh send-event examples/event.example.json
# python3 notification_service.py send-event examples/event.example.json
```

### `GET /api/v1/notifications/{message_id}/status`

Returns aggregated status for a grouped send (or a single message id from the
legacy API). `status` is one of `queued`, `sent`, `delivered`, `failed`, or
`partial` (some channels failed, some succeeded).

```bash
curl http://127.0.0.1:8000/api/v1/notifications/593d453e-928d-4b69-934b-51d8b80cc200/status
```

```json
{
  "success": true,
  "message_id": "593d453e-928d-4b69-934b-51d8b80cc200",
  "reference": "ORD-1234",
  "status": "sent",
  "channels": [
    {
      "message_id": "ca3bb7c6-...",
      "channel": "email",
      "contact": "someone@example.com",
      "status": "sent",
      "provider": "azure_email",
      "provider_message_id": "",
      "error": null,
      "created_at": "2026-08-21T13:12:48.491681+00:00",
      "updated_at": "2026-08-21T13:12:57.587943+00:00"
    }
  ]
}
```

### Legacy API (still supported)

`POST /send` and `GET /status/{message_id}` continue to work unchanged for
existing clients. New code should use `/api/v1`.

## Adding a new channel

1. Add the channel to `Channel` in `app/schemas.py`.
2. Implement `NotificationProvider` (e.g. `TelegramProvider`) in `app/providers/`.
3. Register it in `app/providers/factory.py`.
4. Add the channel to `validate_contact` in `app/validation.py`.

The orchestrator and API need **no changes** — the new channel is available in
`channels[]` immediately.

### 1. `POST /send`

Queues a message for delivery and returns immediately with a `message_id`.

**Request body**

| Field | Type | Notes |
|---|---|---|
| `channel` | string | `whatsapp`, `sms`, or `email` |
| `contact` | string | E.164 phone number for whatsapp/sms (e.g. `+14155551234`), email address for email |
| `message` | string | 1–4096 characters |

**cURL — WhatsApp**

```bash
curl -X POST http://127.0.0.1:8000/send \
  -H "Content-Type: application/json" \
  -d '{
        "channel": "whatsapp",
        "contact": "+14155551234",
        "message": "Hello from the notification service!"
      }'
```

**cURL — SMS**

```bash
curl -X POST http://127.0.0.1:8000/send \
  -H "Content-Type: application/json" \
  -d '{
        "channel": "sms",
        "contact": "+14155551234",
        "message": "Your OTP is 482913"
      }'
```

**cURL — Email**

```bash
curl -X POST http://127.0.0.1:8000/send \
  -H "Content-Type: application/json" \
  -d '{
        "channel": "email",
        "contact": "someone@example.com",
        "message": "Your order has shipped."
      }'
```

**Windows CMD** (escape quotes, keep JSON on one line):

```cmd
curl -X POST http://127.0.0.1:8000/send -H "Content-Type: application/json" -d "{\"channel\": \"sms\", \"contact\": \"+14155551234\", \"message\": \"Hello!\"}"
```

**Response — `202 Accepted`**

```json
{
  "message_id": "b6f1c2a4-3e9d-4b7a-9c1e-2f6a8d0b7e11",
  "status": "queued"
}
```

**Validation error — `400 Bad Request`** (e.g. malformed phone number/email)

```json
{ "detail": "'not-a-number' is not a valid phone number for sms. Use E.164 format, e.g. +14155551234." }
```

### 2. `GET /status/{message_id}`

Returns the current delivery status for a message.

```bash
curl http://127.0.0.1:8000/status/b6f1c2a4-3e9d-4b7a-9c1e-2f6a8d0b7e11
```

**Response — `200 OK`**

```json
{
  "message_id": "b6f1c2a4-3e9d-4b7a-9c1e-2f6a8d0b7e11",
  "channel": "sms",
  "contact": "+14155551234",
  "status": "delivered",
  "provider": "azure_sms",
  "provider_message_id": "mock-4f9a2c7e1d3b",
  "error": null,
  "created_at": "2026-08-19T10:15:00.123456+00:00",
  "updated_at": "2026-08-19T10:15:01.654321+00:00"
}
```

**Not found — `404 Not Found`**

```json
{ "detail": "No message found with id 'unknown-id'" }
```

**Failed send example** (e.g. provider not configured):

```json
{
  "message_id": "...",
  "status": "failed",
  "error": "WhatsApp provider is not configured. Set AZURE_WHATSAPP_CHANNEL_ID in .env."
}
```

## End-to-end CLI test

```bash
# 1. Send a message and capture its ID
MSG_ID=$(./cli.sh send email test@example.com "Hi there" | grep "Message id" | awk '{print $3}')

echo "Queued message: $MSG_ID"

# 2. Check delivery status (wait ~2s for mock mode to mark it delivered)
sleep 2
./cli.sh status $MSG_ID
```

## Status lifecycle

```
queued -> sent -> delivered      (happy path)
queued -> failed                 (validation error, missing credentials, or provider/API error)
```

- `queued`: message accepted and persisted, provider call not yet made.
- `sent`: provider (Azure Communication Services) accepted the message.
- `delivered`: confirmed delivery. In `MOCK_MODE`, this is simulated ~1.5s
  after `sent` so you can observe the full lifecycle without real
  credentials. With real providers, wiring up `delivered` requires the
  provider's delivery-receipt webhook (e.g. Azure webhooks), which
  is a natural next step but outside this service's scope — real sends will
  settle at `sent`.
- `failed`: includes an `error` field with the reason (invalid contact,
  missing provider credentials, network/API error).

## Error handling summary

- **Request validation** (missing/invalid fields, bad channel/contact format) → `400` with a descriptive `detail`.
- **Unknown message ID** → `404`.
- **Provider/config errors** (missing credentials, API/network failure) → message status becomes `failed` with the reason recorded in `error`; the `POST /send` call itself still returns `202` immediately since sending happens asynchronously.
- **Unexpected server errors** → `500` with a generic message (details are logged server-side, not leaked to the client).
