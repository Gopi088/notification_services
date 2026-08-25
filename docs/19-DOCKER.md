# 14 — Docker

## 14.1 Dockerfile

Multi-stage build: compile-only stage (lean) → runtime stage (minimal).

```dockerfile
# Stage 1: build / install
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app .

ENV PATH=/root/.local/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 app && \
    chown -R app:app /app

USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import http.client; c=http.client.HTTPConnection('localhost',8000); c.request('GET','/health'); r=c.getresponse(); exit(0) if r.status==200 else exit(1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 14.2 Docker Compose — Development

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
    restart: unless-stopped

  worker:
    build: .
    command: ["python3", "worker.py"]
    env_file: .env
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
    restart: unless-stopped
    # scale: --scale worker=3

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: notifications
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck: {test: ["CMD", "pg_isready", "-U", "app"], interval: 5s, timeout: 3s, retries: 5}

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--requirepass", "${REDIS_PASSWORD}"]
    ports: ["6379:6379"]
    volumes: ["redisdata:/data"]
    healthcheck: {test: ["CMD", "redis-cli", "ping"], interval: 5s, timeout: 3s, retries: 5}

volumes:
  pgdata:
  redisdata:
```

## 14.3 Docker Compose — Production (minimum)

Add LB/reverse-proxy (nginx/traefik) in front of API, separate worker definition,
secrets via Docker secrets or env file. No volumes for ephemeral workers.

## 14.3b Networking

- Compose creates an internal bridge network for inter-service communication
  (`api`, `worker`, `postgres`, `redis`).
- **Expose only** `api:8000` (and optionally the proxy's `:443`/`:80`) to the host.
- **Do not publish** PostgreSQL (`5432`) or Redis (`6379`) ports to the host in
  production — only API/worker need internal access.
- Use an explicit network definition:

```yaml
networks:
  internal:
    driver: bridge
```

Each service joins `internal`; only the API (or proxy) also binds a host port.

## 14.3c Non-root runtime

- Runtime image creates and switches to a non-root user (`app`, uid 1001) — already
  shown in the Dockerfile (`USER app`).
- PostgreSQL/Redis images already run as non-root when configured (`postgres:16-alpine`
  defaults to the `postgres` user; enforce with `user: "999:999"` or compose `user:`).
- Workers and API never run as root.

## 14.3d Production image differences

- Development image: mounts source as a volume, enables reload, runs tests.
- Production image: static copy of built code, no source mount, `MOCK_MODE=false`,
  `--workers` optional (run one uvicorn process per container; scale via replicas).
- Multi-stage build (Section 14.1) produces the minimal production image.

## 14.4 .dockerignore

```
.env
__pycache__/
*.pyc
.venv/
venv/
notifications.db
.git/
.gitignore
README.md
TEST_PLAN.md
AUTH_AUDIT_DESIGN.md
docs/
examples/
*.md
```

## 14.5 Service Startup Order

1. PostgreSQL (health check passes).
2. Redis (health check passes).
3. API (checks DB + Redis readiness).
4. Worker (checks DB + Redis readiness).

## 14.6 Graceful Shutdown

- Docker `STOPSIGNAL SIGTERM`.
- Worker catches SIGTERM → stop reading, finish in-flight (up to
  `WORKER_GRACE_SECONDS` default 30), then exit.
- API catches SIGTERM → stop accepting new connections, finish in-flight requests.
- `docker-compose down --timeout 60`

## 14.7 Resource Limits

| Service | CPU | Memory |
| ------- | --- | ------ |
| api | 0.5–2 | 256MB–512MB |
| worker | 0.5–4 | 256MB–1GB |
| postgres | 1–2 | 512MB–2GB |
| redis | 0.5–1 | 128MB–512MB |

## 14.8 Image Tagging

- `git describe` (semver + commit) for immutable tags.
- `latest` for convenience (points to latest stable).
- CI: `image:notification-service:${CI_COMMIT_SHA}`