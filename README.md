# Notification Service

A minimal, CLI-only REST API for sending notifications over **WhatsApp**,
**SMS**, and **Email**, and tracking their delivery status. No UI —
everything is driven from the terminal with `curl` (or any HTTP client).

## Project structure

```
notification-service/
├── app/
│   ├── main.py                     # FastAPI app, startup, global error handler
│   ├── config.py                   # Settings loaded from .env
│   ├── database.py                 # SQLite message store (queued/sent/delivered/failed)
│   ├── schemas.py                  # Pydantic request/response models
│   ├── validation.py               # Contact (phone/email) format validation
│   ├── providers/
│   │   ├── base.py                 # NotificationProvider interface + exceptions
│   │   ├── azure_provider.py       # Azure ACS: WhatsApp + SMS + Email
│   │   ├── base.py                 # NotificationProvider interface + exceptions
│   │   └── factory.py              # channel -> provider lookup
│   ├── services/
│   │   └── notification_service.py # Orchestrates send + status updates
│   └── routers/
│       └── notifications.py        # POST /send, GET /status/{message_id}
├── .env.example                    # Copy to .env and fill in credentials
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
| `AZURE_COMMUNICATION_CONNECTION_STRING` | **One connection string covers all 3 channels.** Azure portal → Communication Services → your resource → Keys. |
| `AZURE_DEFAULT_COUNTRY_CODE` | Added to phone numbers that have no country code (default `91`). |
| `AZURE_SMS_FROM` | Your SMS-enabled ACS phone number (E.164), e.g. `+919812345678`. |
| `AZURE_EMAIL_FROM` | Sender verified in Azure Email Communication Service, e.g. `DoNotReply@yourdomain.com`. |
| `AZURE_WHATSAPP_CHANNEL_ID` | WhatsApp channel registration ID (Azure portal → Advanced Messaging). |

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
