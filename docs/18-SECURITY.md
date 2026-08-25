# 12 — Security

## 12.1 Authentication

- API key via `X-API-Key` header.
- `AUTH_ENABLED=false` = dev only; production must enable it.
- Constant-time comparison (`secrets.compare_digest`) — no timing side channels.
- Same 401 body for missing/invalid key (no enumeration).
- Key lifecycle: hashed at rest (SHA-256), optional expiry, revocation
  (`key_expired`/`key_disabled` → 401).
- Webhook endpoints authenticated by HMAC signature
  (`X-EventGrid-Notification-Signature`) — see [AUTH_AUDIT_DESIGN.md](26-AUTH-AUDIT-DESIGN.md).

## 12.2 Authorization

- Scope model: `send:whatsapp`, `send:sms`, `send:email`, `send:any`,
  `read:status`, `admin:keys`.
- Per-channel authorization enforced in the orchestrator before enqueue.
- Deny-by-default; missing scope → 403 `forbidden`.
- Identity propagated to DB rows (`created_by`) and audit log.

## 12.3 Secrets

- **Never** commit provider secrets to source or `.env.example`.
- `.env` is gitignored (already true).
- Provider secrets live in environment variables / secret manager (see 12.8).
- **Never** log secrets: connection strings, `VONAGE_API_SECRET`, webhook secret,
  `AUTH_API_KEY`, raw `X-API-Key`.
- Log only presence flags (`vonage_api_key_loaded=true`) and redacted values
  (central `_redact()` reuse).

## 12.4 Encryption in Transit

- HTTPS/TLS for all public endpoints (terminated at LB / reverse proxy).
- All provider calls use HTTPS (enforced: Vonage sandbox HTTPS, Azure HTTPS).
- Attachments: only `https://` URLs accepted; redirects disallowed
  (already enforced in `AzureEmailProvider._validate_url`).

## 12.5 Input Validation

- Pydantic schema validation on all request bodies.
- `validate_contact` for phone (E.164-ish) and email.
- Template name path-traversal protection (`Path(name).name`).
- Attachment SSRF guards: no private/loopback/link-local/reserved IPs,
  no embedded credentials, no redirects (already implemented).
- Parameterized SQL (no string interpolation).

## 12.6 PII Handling

- Notification messages may contain PII. Policy:
  - Not logged at INFO; at DEBUG email **subject** only, never full bodies.
  - Audit log stores `message_id`, not content.
  - Database access is restricted; encryption at rest optional layer.
- Phone numbers/emails masked in logs where possible (e.g., `+919887****48`).

## 12.7 Database & Redis Credentials

- PostgreSQL and Redis use strong passwords, stored as secrets, never in code.
- Separate application DB user with least privilege (no superuser).
- Redis: `requirepass`, bind to private network; TLS for remote access.
- Connections use timeouts and pool limits.

## 12.8 Production Secret Injection

- Dev: `.env` (gitignored).
- Docker: secrets via Docker secrets / env files (never baked into image).
- Cloud: AWS Secrets Manager / Azure Key Vault / GCP Secret Manager injected at
  runtime; environment variables reference the secret manager.
- CI: secrets from the CI provider's secret store.

## 12.9 Least Privilege

- API key scopes limit blast radius (a `read:status`-only key cannot send).
- DB user has only the privileges the app needs.
- Workers and API use distinct credentials if isolated.

## 12.10 Dependency Security

- Pin dependency versions (existing `requirements.txt` pins exact versions).
- Regular `pip-audit` / `osv-scanner` in CI.
- Keep base images patched; use `latest`-tag discipline or digest pinning.

## 12.11 Security Tests

- Secrets never in API responses or logs.
- 401/403 behavior.
- SSRF guards.
- Injection-style inputs rejected.
- PII masking.
- Rate-limit abuse.
- See [TEST_PLAN.md](27-TEST-PLAN.md) security section (TC-143..TC-147) and
  [AUTH_AUDIT_DESIGN.md](26-AUTH-AUDIT-DESIGN.md).