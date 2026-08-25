# 18 — CI/CD

## 18.1 Pipeline

```
Git push
  ↓
Lint (ruff / mypy)
  ↓
Unit tests
  ↓
Integration tests
  ↓
Coverage (fail < 90%)
  ↓
Security checks (pip-audit / bandit)
  ↓
Build Docker image (multi-stage)
  ↓
Container tests (smoke: /health)
  ↓
Push image (immutable tag)
  ↓
Deploy staging
  ↓
Production approval (manual gate)
  ↓
Deploy production (rolling, health-gated)
```

## 18.2 CI Stages

| Stage | Tool | Fails on |
| ----- | ---- | -------- |
| Lint | `ruff check` + `mypy` | any lint/type error |
| Unit tests | `pytest tests/unit` | any failure |
| Integration tests | `pytest tests/integration` (containers for PG/Redis) | any failure |
| Coverage | `pytest --cov=app --cov-fail-under=90` | coverage < 90% |
| Security | `pip-audit`, `bandit`, `gitleaks` (secret scan) | vulnerable/misconfigured deps, secrets in repo |
| Build | `docker build` (multi-stage) | build failure |
| Container smoke | run container, curl `/health` | non-200 |
| Push | `docker push` immutable tag | — |

## 18.3 Coverage Gate

- Hard gate: `--cov-fail-under=90` blocks merge.
- Four metrics targeted: statements, branches, functions, lines ≥ 90%.
- Report artifacts: HTML + `coverage.xml` (Cobertura) for PR comments.

## 18.4 Secret Scanning

- `gitleaks` (or `trufflehog`) runs on every push.
- Blocks if any secret pattern (AWS keys, connection strings, `VONAGE_API_SECRET`)
  appears in the diff.
- `.env`, `*.key`, credentials files are gitignored and scanned.

## 18.5 Environments

| Env | Trigger | Secrets source |
| --- | ------- | -------------- |
| CI | every push / PR | CI secret store |
| Staging | merge to `main` (auto) | CI secret store (staging namespace) |
| Production | manual approval gate | Secret Manager (prod) |

## 18.6 Deploy Automation

- Staging: auto-deploy from `main`.
- Production: approve in CI UI, rolling deploy with health gates, automatic
  rollback on readiness failure.
- Migrations: run forward-only migration job before API/worker rollout.

## 18.7 Notes

- No CI exists in the repository today (verified). This document defines what
  to add during implementation (Phase 12 in
  [24-IMPLEMENTATION-PLAN.md](24-IMPLEMENTATION-PLAN.md)).
- Do not run real sends in CI; provider calls are mocked. Staging may use
  sandbox providers only.