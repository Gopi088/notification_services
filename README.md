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

The service supports **SQLite** (local dev), **PostgreSQL** (Docker/local), and
**CockroachDB Cloud** (wire-compatible with PostgreSQL).

| Backend | `STORAGE_BACKEND` | `DATABASE_BACKEND` | `DATABASE_URL` |
| ------- | ----------------- | ------------------ | -------------- |
| SQLite | `sqlite` | `postgres` | (unused) |
| PostgreSQL | `postgres` | `postgres` | `postgresql://user:pass@host:5432/db` |
| CockroachDB Cloud | `postgres` | `cockroachdb` | `postgresql://user:pass@host:26257/db?sslmode=verify-full` |

For CockroachDB Cloud, also set `COCKROACH_CA_CERT` to the path of the CA
certificate downloaded from the CockroachDB Cloud console (required for
`sslmode=verify-full` TLS). The connection string and password are read only
from the environment and are never logged.

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