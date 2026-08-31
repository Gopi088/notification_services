# Notification System

All project documentation has been consolidated into the `docs/` directory.

## Entry point

Start at [docs/README.md](docs/README.md).

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with SQLite (dev)
MOCK_MODE=true venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# Run tests
venv/bin/python -m pytest tests/ --cov=app --cov-report=term --cov-fail-under=90

# Run with Docker
docker build -t notification-service .
docker compose up
```

## Databases

The service supports **SQLite** (local development) and **PostgreSQL**
(Docker or production).

| Backend | `STORAGE_BACKEND` | `DATABASE_URL` |
| ------- | ----------------- | -------------- |
| SQLite | `sqlite` | (unused) |
| PostgreSQL | `postgres` | `postgresql://user:pass@host:5432/db` |

```bash
# Verify a database connection without printing credentials
python3 notification_service.py db-check
```

See [docs/28-MESSAGE-ROUTING-TEMPLATES-ERRORS.md](docs/28-MESSAGE-ROUTING-TEMPLATES-ERRORS.md)
and [docs/25-REDIS-DESIGN.md](docs/25-REDIS-DESIGN.md) for details.

## Channels

- SMS: working (Vonage preferred, Azure fallback)
- WhatsApp: working (Vonage Sandbox preferred, Azure fallback)
- Email: working (Azure)

## Status

- Coverage: **>= 90%**
- Tests: all passing
- State: production-grade architecture with queue + workers + PostgreSQL + Redis

## Configuration

All settings are read from environment variables (AWS task/env vars, `.env`,
or compose overrides) — nothing is hardcoded, and changing them never requires
rebuilding the image. The full list lives in [`.env.example`](.env.example).

Key operational variables:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `HOST` | `127.0.0.1` | Bind address for the API server (`run.sh`). Use `0.0.0.0` inside Docker to expose on all interfaces. |
| `PORT` | `8000` | Port the API server listens on (`run.sh`). If the port is busy, `run.sh` fails with a clear message telling you how to change it. |
| `AUTH_ENABLED` | `false` | Set `true` to require JWT auth on `/api/v1/*`. |
| `JWT_SECRET_KEY` | *(empty)* | Signing secret for access tokens (`secrets.token_hex(32)`). Never hardcode. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access-token lifetime. |
| `AUTH_CLIENT_ID` / `AUTH_CLIENT_SECRET` | `notification-service` / *(empty)* | Login credentials for `POST /api/v1/auth/login`. Falls back to `AUTH_API_KEY`. |
| `LOG_LEVEL` | `INFO` | Application log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Use `DEBUG` only while troubleshooting (higher volume). |
| `LOG_FORMAT` | `text` | `text` (human/colored terminal) or `json` (structured lines for log aggregation). |
| `LOG_FILE` | *(empty)* | Optional rotating file path for application logs (in addition to stdout). |
| `AUDIT_LOG_FILE` | *(empty)* | Optional JSON-lines file for audit events (durable even if the DB is down). |
| `DUPLICATE_WINDOW_MINUTES` | `30` | Duplicate-detection window: the same user + channel + recipient + content/template sent again within this many minutes is blocked with an "already sent recently, do you want to resend?" response and returns the existing message id. Outside the window it is a new notification. `0` disables. Examples: `30`, `60`, `120`. |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | TTL for client-supplied `Idempotency-Key` headers. |
| `MOCK_MODE` | `true` | `false` in production (real provider calls). |
| `STORAGE_BACKEND` | `sqlite` | `sqlite` (dev) or `postgres` (production). |
| `DATABASE_URL` | *(empty)* | PostgreSQL DSN when `STORAGE_BACKEND=postgres`. Never logged. |
| `QUEUE_ENABLED` | `false` | `true` in production (Redis Streams + workers). |

### Logging behaviour

- **INFO** is the production default — one line per meaningful lifecycle event.
- **DEBUG** adds the full request lifecycle trace: auth → validation →
  idempotency → DB → queue → worker → provider → retry → status → webhook.
- API keys, secrets, passwords, `Authorization` headers, `DATABASE_URL` and
  message content are **never** logged — secret field names are masked and PII
  (phones/emails) is partially masked.
- Audit events are recorded **separately** from application logs (PostgreSQL +
  optional `AUDIT_LOG_FILE`) and are never suppressed by `LOG_LEVEL`.

### JWT authentication

Set `AUTH_ENABLED=true` and `JWT_SECRET_KEY` to protect `/api/v1/*` with
Bearer JWTs:

```bash
# 1. Obtain a token
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"client_id": "notification-service", "client_secret": "your-secret"}'
# -> {"access_token": "<jwt>", "token_type": "bearer", "expires_in": 1800}

# 2. Use it on protected routes
curl -X POST http://127.0.0.1:8000/api/v1/notifications/send \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"channels": [{"channel": "sms", "contact": "+919887270348"}], "message": "Hello"}'

# Status (also protected)
curl http://127.0.0.1:8000/api/v1/notifications/<id>/status \
  -H "Authorization: Bearer <jwt>"
```

- Missing/expired/invalid tokens return **401**.
- The `user_id` claim drives authorization, audit logs, idempotency and
  rate limiting.
- Webhook/provider auth (Twilio signatures, Azure Event Grid) is separate
  from client JWT auth.

### Candidate message report

`GET /api/v1/reports/candidates/{candidate_id}` (auth-protected) returns a
delivery report for a candidate/contact: total messages, counts by channel and
status, and the detailed notification records. A candidate is identified by
their contact/recipient; the report is computed from the existing notification
records (no separate reporting table).

```bash
curl "http://127.0.0.1:8000/api/v1/reports/candidates/+919887270348" \
  -H "Authorization: Bearer <jwt>"
```
Optional `limit` (default 50, max 100) and `offset` for pagination. When auth
is enabled, only the authenticated user's own records are returned.

### Resend / duplicate handling

- Same user + channel + recipient + content/template inside
  `DUPLICATE_WINDOW_MINUTES` → duplicate response (no auto-send).
- `resend=true` / `force_resend=true` → creates a NEW notification with a new
  message id and sends it; the original is never overwritten.

### Message status lifecycle

The service tracks a provider-independent status for every message:

```
queued → processing → submitted → delivered / failed
```

Additional states where supported:

| Status | Meaning |
| ------ | ------- |
| `queued` | Accepted by the API, waiting for a worker |
| `processing` | Worker is calling the provider |
| `submitted` | Provider accepted the request (NOT delivered) |
| `sent` | Provider confirmed it was sent to the carrier |
| `delivered` | Provider/webhook confirmed delivery to the recipient |
| `read` | Recipient read the message (whatsapp only, when available) |
| `failed` | Delivery failed (permanent error) |
| `retrying` | Temporary failure, will be retried |
| `acknowledged` | Recipient replied / acknowledged |

The status API (`GET /api/v1/notifications/{id}/status`) returns:

- `message_id`, `channel`, `contact`, `status`
- `provider`, `provider_message_id`, `error`
- `retry_count`, `created_at`, `updated_at`
- `delivered_at`, `read_at`, `acknowledged_at`
- `elapsed_seconds`, `delivery_timeout_seconds`, `timed_out`

Invalid backward transitions (e.g. `delivered → queued`) are rejected. Status
updates come from provider webhooks where available; polling is not required.

### On-demand delivery polling

Besides webhooks, checking a status advances the message when the provider
supports it: each `GET .../status` call asks the provider (Twilio) for the real
delivery state of a message still in `submitted`/`sent`, and persists any valid
forward transition. So a Twilio SMS/WhatsApp shows `delivered` (and stamps
`delivered_at`) as soon as Twilio confirms it reached the recipient's network —
no webhook URL needed for this path.

### Delivery webhooks

Status past `submitted` is driven by provider delivery callbacks:

| Channel | Webhook | What it updates |
| ------- | ------- | --------------- |
| SMS (Twilio) | `POST /api/v1/twilio/sms/status` (`TWILIO_SMS_STATUS_CALLBACK_URL`) | `submitted`, `delivered`, `failed` |
| WhatsApp (Twilio) | `POST /api/v1/twilio/whatsapp/status` (`TWILIO_WHATSAPP_STATUS_CALLBACK_URL`) | `submitted`, `delivered`, `read`, `failed` |
| WhatsApp (Azure) | `POST /api/v1/whatsapp/webhook` | `sent`, `delivered`, `read`, `failed` |
| Email (Azure) | `POST /api/v1/whatsapp/webhook` (`EmailDeliveryReportReceived`) | `delivered`, `failed` |

Legacy Twilio aliases `POST /api/v1/twilio/status` and `POST /api/v1/sms/webhook`
still work. Configure the URLs in `.env` (or the provider console); use
`ngrok http 8000` during development. `read_at` and `acknowledged_at` only
appear for channels that support read receipts / replies (WhatsApp); email/SMS
leave them `null`, which is expected.

Delivery webhook URLs are configurable per channel:
`TWILIO_SMS_STATUS_CALLBACK_URL`, `TWILIO_WHATSAPP_STATUS_CALLBACK_URL`
(fallbacks: `SMS_STATUS_WEBHOOK_URL` / `WHATSAPP_STATUS_WEBHOOK_URL`, then
`TWILIO_STATUS_CALLBACK_URL`). On-demand polling is a fallback only when
`DELIVERY_POLLING_ENABLED=true` — `delivered` always comes from an actual
provider confirmation, never from a timeout.

For Azure email, create an Event Grid subscription for
`Microsoft.Communication.EmailDeliveryReportReceived` targeting the public
HTTPS `/api/v1/email/webhook` URL. `submitted` remains correct until Azure
sends that receipt: it means Azure accepted the email, not that the recipient
mail server accepted it. Azure sends `Delivered` when it hands the email to
the recipient mail-transfer agent; bounce, spam, quarantine, suppression, and
failure reports become `failed` here.
