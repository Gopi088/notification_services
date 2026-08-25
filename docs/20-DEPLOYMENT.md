# 15 — Deployment

## 15.1 Deployment Stages

```
Development (local, Docker Compose)
   ↓
Staging (single VM or Docker host, real creds + sandbox providers)
   ↓
Production (managed service(s) or Docker Swarm, real providers)
```

Pragmatic progression: do **not** start with Kubernetes. Start with Docker
Compose on a VM, then move to a managed container platform or cloud services as
load requires.

## 15.2 Docker Compose (single host, dev/staging)

- One host running `docker compose up -d`.
- Services: api (scaled), worker (scaled), postgres, redis, reverse proxy
  (nginx/traefik) terminating TLS.
- Secrets: `.env` file (staging) or Docker secrets (production).
- Suitable up to ~10k users / modest throughput.

## 15.3 VM / Managed Host

- For staging: one VM with Docker.
- For production (low-mid scale): 2+ VMs:
  - VM group A: API replicas behind an LB.
  - VM group B: workers.
  - Managed PostgreSQL + Managed Redis (remove self-management).

## 15.4 Cloud Managed Services (recommended production path)

| Component | AWS | Azure | GCP |
| --------- | --- | ----- | --- |
| API/Worker | ECS Fargate / EKS | AKS / App Service | Cloud Run / GKE |
| PostgreSQL | RDS | Azure Database for PostgreSQL | Cloud SQL |
| Redis | ElastiCache | Azure Cache for Redis | Memorystore |
| Queue | ElastiCache Redis Streams | Azure Cache Redis Streams | Memorystore Redis Streams |
| Secrets | Secrets Manager | Key Vault | Secret Manager |
| Load balancer | ALB | Azure Load Balancer | Global LB |
| Logs/metrics | CloudWatch | Azure Monitor | Cloud Logging/Monitoring |

## 15.5 Kubernetes (when justified)

Adopt Kubernetes only when: > ~100k users, need for autoscaling across AZs,
multi-region, or organizational mandate. Documented as an option; not the
default start.

## 15.6 Environment Configuration

| Env var group | Examples |
| ------------- | -------- |
| App | `MOCK_MODE`, `LOG_LEVEL`, `LOG_FORMAT`, `AUTH_ENABLED`, `AUTH_API_KEY` |
| PostgreSQL | `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` |
| Redis | `REDIS_URL`, `REDIS_PASSWORD` |
| Vonage | `VONAGE_API_KEY`, `VONAGE_API_SECRET`, `VONAGE_SMS_FROM`, `VONAGE_WHATSAPP_FROM`, `VONAGE_WHATSAPP_SANDBOX_URL` |
| Azure | `COMMUNICATION_SERVICES_CONNECTION_STRING`, `AZURE_SMS_FROM`, `AZURE_EMAIL_FROM`, `WHATSAPP_CHANNEL_ID`, `WHATSAPP_TEMPLATE_NAME` |
| Queue/worker | `QUEUE_*`, `WORKER_CONCURRENCY_*`, `WORKER_GRACE_SECONDS` |
| Observability | `OTEL_*` (optional) |

All secrets come from the secret manager; `.env` files are dev-only and gitignored.

## 15.7 HTTPS / Domain / Load Balancer

- TLS terminated at reverse proxy / managed LB.
- Domain → LB → API replicas.
- Readiness probe gates LB traffic.

## 15.8 Independent Scaling

- **API** scales by requests/sec (horizontal replicas; stateless).
- **Workers** scale by queue lag (horizontal replicas; share consumer group).
- Database scales vertically first, then read replicas for status reads.
- Redis scales by memory/connections (cluster when needed).

## 15.9 Rollout / Rollback

- Immutable image tags; rolling updates with health gates.
- Rollback = redeploy previous image tag.
- DB migrations: forward-only with backward-compatible columns; run migration
  before deploying new API/worker code.