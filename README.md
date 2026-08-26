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
