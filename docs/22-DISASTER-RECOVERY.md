# 17 — Disaster Recovery

## 17.1 Failure Scenarios & Response

| Failure | Detection | Response | Recovery |
| ------- | --------- | -------- | -------- |
| PostgreSQL failure | readiness probe fails, DB alerts | API 503 (no enqueue); workers retry DB ops | Restore from backup / failover to replica; reconciliation requeues orphaned `queued` rows |
| Redis failure | `redis_available` metric, alerts | rate limit fails open; idempotency falls back to PG; queue consumers retry reads | Redis restarts with AOF; streams resume; no permanent loss (PG is source of truth) |
| Queue (streams) failure | lag/depth alerts | API may still enqueue (fails to XADD → 503) | Resume XADD; reconciliation requeues `queued` rows |
| API crash | health/LB | LB routes around; in-flight requests lost (harmless — idempotency) | Restart; replicas |
| Worker crash | worker down alert | pending messages reclaimed via XAUTOCLAIM | Restart worker; messages reprocessed idempotently |
| Provider outage | provider error rate | retries with backoff; circuit breaker; DLQ after max attempts | Automatic when provider recovers; DLQ re-queue via retry endpoint |
| Network outage | timeouts | retryable classification; backoff | Automatic |
| Data corruption | integrity checks, alerts | failover / restore | Point-in-time restore; audit chain verification |

## 17.2 Backups

- **PostgreSQL:** continuous WAL archiving + daily full backups.
  - Point-in-time recovery (PITR) enabled.
  - Backup retention: 30 days (adjust per compliance).
  - Restore drills run on schedule (e.g., quarterly) to validate RTO/RPO.
- **Redis:** AOF (`appendfsync everysec`) is the recovery mechanism; Redis is
  rebuildable from PostgreSQL, so backups are secondary.
- **Config/secrets:** stored in secret manager with versioning; deploy config
  is reproducible from Git + env.

## 17.3 Restore Strategy

1. Provision new PostgreSQL (or failover replica).
2. Restore latest backup + replay WAL to target time.
3. Point API/workers at restored DB (env change + rolling restart).
4. Reconciliation job requeues `queued`/`retrying` rows.
5. Verify status counts match pre-incident expectations.

## 17.4 RPO / RTO Targets

| Metric | Target | Rationale |
| ------ | ------ | --------- |
| RPO (PostgreSQL) | ≤ 5 min | WAL streaming provides near-real-time point-in-time recovery; 5 min is comfortably achievable with PITR. |
| RPO (queue) | ≤ 1 min | AOF everysec bounds stream loss; PG reconciliation self-heals beyond that. |
| RTO | ≤ 30 min | Restore/rebuild of managed services within half an hour is realistic for a service of this size without multi-region active-active. |

These are initial targets; tighten later if compliance/UX requires.

## 17.5 Recovery Process

1. Declare incident; freeze deploys.
2. Assess scope (DB / Redis / providers).
3. Execute restore or failover per runbook.
4. Run reconciliation (requeue queued/retrying).
5. Replay dead-letter queue where safe.
6. Verify metrics + sample statuses.
7. Post-incident review; update runbooks.

## 17.6 Prevention / Mitigation

- Multi-AZ PostgreSQL (managed service).
- Redis with AOF + replicas.
- Idempotency + retries absorb transient failures.
- Readiness gating prevents routing traffic to unhealthy replicas.
- Regular backup restore drills.