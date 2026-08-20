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
│   │   ├── whatsapp_provider.py    # Twilio WhatsApp integration
│   │   ├── sms_provider.py         # Twilio SMS integration
│   │   ├── email_provider.py       # SMTP email integration
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
# Edit .env with your Twilio / SMTP credentials.
# Leave MOCK_MODE=true to test the whole flow with zero credentials.

# 4. Run the API
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# or: bash run.sh
```

The API is now at `http://127.0.0.1:8000`. Interactive docs (Swagger UI) are
available at `http://127.0.0.1:8000/docs` if you want to explore it in a
browser, but every operation below works purely from the command line.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `MOCK_MODE` | `true` = simulate sends, no credentials needed (default). `false` = use real providers below. |
| `DATABASE_PATH` | SQLite file used to track message status. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio credentials, used for both WhatsApp and SMS. |
| `TWILIO_WHATSAPP_FROM` | Your Twilio WhatsApp sender, e.g. `whatsapp:+14155238886`. |
| `TWILIO_SMS_FROM` | Your Twilio SMS-enabled phone number. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_TLS` | SMTP relay used for the email channel (works with Gmail app passwords, SES SMTP, Mailgun SMTP, etc). |

## API

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
  "provider": "twilio_sms",
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
  "error": "WhatsApp provider is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM in .env."
}
```

## End-to-end CLI test

```bash
# 1. Send a message and capture its ID (requires jq: apt/brew install jq)
MSG_ID=$(curl -s -X POST http://127.0.0.1:8000/send \
  -H "Content-Type: application/json" \
  -d '{"channel":"email","contact":"test@example.com","message":"Hi there"}' \
  | jq -r .message_id)

echo "Queued message: $MSG_ID"

# 2. Poll status until it settles
sleep 2
curl -s http://127.0.0.1:8000/status/$MSG_ID | jq
```

## Status lifecycle

```
queued -> sent -> delivered      (happy path)
queued -> failed                 (validation error, missing credentials, or provider/API error)
```

- `queued`: message accepted and persisted, provider call not yet made.
- `sent`: provider (Twilio/SMTP) accepted the message.
- `delivered`: confirmed delivery. In `MOCK_MODE`, this is simulated ~1.5s
  after `sent` so you can observe the full lifecycle without real
  credentials. With real providers, wiring up `delivered` requires the
  provider's delivery-receipt webhook (e.g. Twilio status callbacks), which
  is a natural next step but outside this service's scope — real sends will
  settle at `sent`.
- `failed`: includes an `error` field with the reason (invalid contact,
  missing provider credentials, network/API error).

## Error handling summary

- **Request validation** (missing/invalid fields, bad channel/contact format) → `400` with a descriptive `detail`.
- **Unknown message ID** → `404`.
- **Provider/config errors** (missing credentials, API/network failure) → message status becomes `failed` with the reason recorded in `error`; the `POST /send` call itself still returns `202` immediately since sending happens asynchronously.
- **Unexpected server errors** → `500` with a generic message (details are logged server-side, not leaked to the client).
