# Baseline — Notification Service

Recorded before the redesign. Date: 2026-08-25.

## Architecture

**Single-process FastAPI** (Python 3.12) backed by **SQLite** with in-process
`BackgroundTasks` dispatch. Three channels, all working.

## Current Architecture (after redesign)

The project has been redesigned into a production-grade architecture:

- PostgreSQL (durable source of truth) via `app/storage.py`
- Redis Streams (message queue) via `app/queue.py`
- Workers (async delivery) via `app/worker.py`, `app/worker_runner.py`
- Rate limiting via `app/ratelimit.py`
- Idempotency via `app/idempotency.py`
- Retry via `app/retry.py`
- Structured logging via `app/logging_config.py`
- Health/readiness/liveness via `app/main.py`
- Dockerfile + docker-compose.yml

## Test Status

| Metric | Value |
| ------ | ----- |
| Test files | 27 (pytest) |
| Tests | ~150 passing |
| Coverage (app) | **90.01%** |
| Coverage gate | **--cov-fail-under=90 passes** |

## Working Channels

| Channel | Provider | Status |
| ------- | -------- | ------ |
| SMS | VonageSMSProvider (preferred), AzureSMSProvider (fallback) | Working |
| WhatsApp | VonageWhatsAppProvider (preferred), AzureWhatsAppProvider (fallback) | Working |
| Email | AzureEmailProvider | Working |

## Docker

```
docker build -t notification-service .
docker compose up   # starts api, worker, postgres, redis
```

## Commands

```bash
# Run API server
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# Run tests
venv/bin/python -m pytest tests/ test_webhooks.py test_vonage_whatsapp.py --cov=app --cov-fail-under=90

# Generate coverage
venv/bin/python -m pytest tests/ --cov=app --cov-report=html

# CLI
python3 notification_service.py
```

## Documentation

All documentation is in `docs/`. Start at `docs/README.md`.