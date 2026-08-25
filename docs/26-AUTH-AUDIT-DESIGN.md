# AUTHENTICATION, AUTHORIZATION, LOGGING & AUDIT — DESIGN

**Project:** Notification Service (`notification-service`)
**Version:** 2.0.0
**Status:** Design / specification document (implementation to follow)
**Companion:** see `TEST_PLAN.md` for the automated test strategy of everything below.

---

## 1. Overview

This document specifies the complete **authentication (authn)**, **authorization (authz)**,
**logging**, and **audit logging** design for the Notification Service.

The service currently has a minimal single-API-key guard and basic plain-text logging,
and no audit trail. This document defines:

1. **Authentication** — proving *who* the caller is (API key identity).
2. **Authorization** — proving *what* that identity may do (scopes/permissions per channel).
3. **Logging** — operational, structured, correlation-aware, secret-safe logs.
4. **Audit log** — an append-only, tamper-evident record of security-relevant actions.

The design is intentionally layered so the current behavior remains fully compatible
when the first enhancement is shipped (see Section 9. Backward Compatibility).

---

## 2. Current State Analysis

### 2.1 What exists today

| Area | Current implementation |
| ---- | ---------------------- |
| Authentication | `app/auth.py::require_api_key` — a single static API key. Header `X-API-Key`. Active only when `AUTH_ENABLED=true`. Constant-time compare via `secrets.compare_digest`. |
| Authorization | **None.** A valid key grants full access to every endpoint and every channel. No scopes, no roles, no per-channel restriction, no rate limiting. |
| Logging | `logging.basicConfig(level=INFO)` in `app/main.py`. Module loggers: `app`, `orchestrator`, `azure_provider`, `vonage_provider`, `webhooks`. Plain text format `%(asctime)s %(levelname)s %(name)s: %(message)s`. |
| Audit log | **None.** No record of who did what, when, with which key, or which outbound notification was sent by which caller. |
| Correlation | **None.** No request ID propagated from API request → orchestrator → provider → database. |
| Secrets handling | Connection strings / API secrets live in `.env` (gitignored). Providers never log the secret. `_redact()` in `webhooks.py` masks secret-like keys in event logs. |

### 2.2 Exposed endpoints

| Method | Path | Auth today | Notes |
| ------ | ---- | ---------- | ----- |
| POST | `/api/v1/notifications/send` | `require_api_key` | multi-channel send |
| POST | `/api/v1/notifications/event` | `require_api_key` | event-driven send |
| GET | `/api/v1/notifications/{id}/status` | `require_api_key` | status |
| GET | `/api/v1/health` | `require_api_key` | health/config |
| POST | `/send` | `require_api_key` | legacy send |
| GET | `/status/{message_id}` | `require_api_key` | legacy status |
| GET | `/health` | none | unauthenticated liveness |
| POST | `/api/v1/whatsapp/webhook` | none (HMAC-protected) | Event Grid delivery receipts |
| GET | `/api/v1/whatsapp/webhook` | none | Event Grid validation handshake |

### 2.3 Current configuration (relevant keys)

```
AUTH_ENABLED
AUTH_API_KEY
MOCK_MODE
DATABASE_PATH
DELIVERY_TIMEOUT_SECONDS
```

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Identify every caller that reaches protected endpoints.
- Restrict actions to what the caller is permitted to do (per-channel, read vs write).
- Produce **structured**, **correlated**, **searchable** operational logs.
- Produce an **append-only**, **tamper-evident** audit trail of security-relevant events.
- Never log or expose secrets, PII beyond necessity, or full notification bodies in audit logs.
- Keep the changes incremental and backward compatible with the existing API contract.

### 3.2 Non-Goals (out of scope for this design)

- Full OAuth 2.0 / OpenID Connect provider integration (documented as a future option in 10.8).
- Multi-tenant user management / SSO.
- Network-level security (TLS termination, WAF) — assumed handled by infrastructure.
- Message-content encryption at rest (can be layered later).

---

## 4. Authentication Design

### 4.1 Authentication levels

| Level | Name | When used |
| ----- | ---- | --------- |
| `AUTH_LEVEL=none` | No authentication | Public endpoints only: `GET /health`, webhook validation/ingest (HMAC-signed). |
| `AUTH_LEVEL=api_key` (default) | Static API key | Current behavior; one shared key. |
| `AUTH_LEVEL=keys` | Managed API keys | Multiple keys, each with identity, scopes, optional expiry (recommended target). |

`AUTH_ENABLED` continues to be the master switch:

- `AUTH_ENABLED=false` → all protected endpoints open (dev/local only).
- `AUTH_ENABLED=true` → authentication **and** authorization enforced.

### 4.2 API key model (target)

Replace the single `AUTH_API_KEY` with a **key table** while keeping `AUTH_API_KEY`
working as a legacy shorthand.

**Key properties**

| Field | Description |
| ----- | ----------- |
| `key_id` | Public identifier (`key_live_<16 hex>`), safe to log. |
| `key_hash` | SHA-256 (or Argon2id) hash of the secret — **never store the raw key**. |
| `name` | Human label, e.g. `staging-integration`. |
| `scopes` | Comma-separated permission set (see Section 5). |
| `expires_at` | Optional expiry (ISO-8601). Empty = no expiry. |
| `enabled` | Active/inactive flag. |
| `created_at` / `revoked_at` | Lifecycle timestamps. |

**Key format for callers**

```
X-API-Key: live_<id>.<secret>        # single header value
```

The server splits on the first `.`, looks up `key_id`, compares the hash of `secret`
with a constant-time comparison (`secrets.compare_digest` on hex digests).

**Authentication flow**

```
Client → X-API-Key: live_<id>.<secret>
        ↓
require_api_key dependency
  ├─ AUTH_ENABLED=false        → allow (no identity)
  ├─ AUTH_LEVEL=api_key        → compare against AUTH_API_KEY (legacy)
  └─ AUTH_LEVEL=keys
      ├─ parse key_id + secret
      ├─ lookup key_id in DB   → missing ⇒ 401
      ├─ key enabled?          → disabled ⇒ 401
      ├─ key expired?          → expired ⇒ 401
      ├─ hash(secret) matches? → constant-time compare ⇒ else 401
      └─ identity + scopes attached to request state
        ↓
audit event: authentication.success / authentication.failure (see Section 7)
```

**Failed attempts are not differentiated** in responses (identical 401 + `WWW-Authenticate: ApiKey`)
to avoid leaking which part failed (user enumeration / key-enumeration resistance).

### 4.3 Key management operations

Provided as a small internal CLI (`python3 notification_service.py keys <subcommand>`) —
not exposed over HTTP:

- `keys create --name <label> --scopes send:whatsapp,send:sms,read:status`
- `keys revoke <key_id>`
- `keys list`
- `keys expire <key_id> --at <iso>`

These operations are themselves **audited**.

### 4.4 Webhook authentication

Delivery receipts (`POST /api/v1/whatsapp/webhook`) are **not** protected by API keys;
they are authenticated by **HMAC-SHA256 signature** over the raw request body using a
shared webhook secret:

```
X-EventGrid-Notification-Signature: sha256=<hex>
```

- New config: `WHATSAPP_WEBHOOK_SECRET` (default empty = signature not required, for
  compatibility; set to enforce).
- Constant-time signature verification; failed signatures → `401` + audit `webhook.auth_failed`.
- Event Grid validation handshake (`GET`) remains public but is **logged and audited**.

---

## 5. Authorization Design

### 5.1 Scope model

Permissions are expressed as `resource:action` scopes attached to a key.

| Scope | Grants |
| ----- | ------ |
| `send:whatsapp` | POST `/api/v1/notifications/send` / `/event` for channel `whatsapp` |
| `send:sms` | same, for channel `sms` |
| `send:email` | same, for channel `email` |
| `send:any` | send over any channel |
| `read:status` | GET status endpoints (v1 + legacy) |
| `read:audit` | internal read of audit records (admin tooling) |
| `webhook:write` | allowed to post delivery-receipt webhook payloads (HMAC signed) |
| `admin:keys` | key lifecycle management (CLI only) |

### 5.2 Enforcement points

1. **Channel-level authorization** in `orchestrate_send` / `orchestrate_event`:
   each channel in the request is checked against the caller's scopes **before** anything
   is queued. A request with `send:sms` calling a 2-channel payload `whatsapp + sms` is
   rejected with `403` and audited.
2. **Endpoint-level authorization** via FastAPI dependency (`require_scope(...)`):
   - `read:status` for status endpoints.
   - `send:*` for send/event endpoints.
3. **Legacy routes** map to the same checks (legacy `/send` requires `send:any`).

### 5.3 Deny-by-default

- Unknown scope, empty scope list, or disabled key ⇒ `403 Forbidden`.
- Missing scope for a requested channel ⇒ `403` with `error.code = "forbidden"`.
- No authorization short-circuits if `AUTH_ENABLED=false` (dev mode), identical to today.

### 5.4 Identity propagation

The resolved identity (`key_id`, `key_name`, `scopes`) is attached to the request state
(`request.state.auth`) so that:

- Orchestrator can tag every DB row with `created_by = key_id`.
- Audit log can record the actor.
- Providers/logs can reference the caller without exposing the raw key.

---

## 6. Logging Design

### 6.1 Goals

- **Structured** output (JSON) for machine parsing and log-aggregation (e.g. Loki, ELK).
- **Correlated** across request → orchestrator → provider → DB via a `request_id` (and `group_id`).
- **Level-appropriate**, configurable verbosity.
- **Secret-safe** by construction (central redaction; never log connection strings, keys,
  full message bodies by default).

### 6.2 Configuration

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `LOG_LEVEL` | `INFO` | Root log level: `DEBUG/INFO/WARNING/ERROR/CRITICAL` |
| `LOG_FORMAT` | `json` | `json` (structured) or `text` (plain, for local dev) |
| `LOG_FILE` | *(empty)* | Optional file path; empty = stderr |
| `LOG_REDACT_KEYS` | *(default set)* | Extra field names to redact |

### 6.3 JSON log shape

```json
{
  "ts": "2026-08-24T17:30:00.123Z",
  "level": "info",
  "logger": "orchestrator",
  "request_id": "req_4f1a...",
  "group_id": "7f21ab9c-...",
  "message_id": "c349881a-...",
  "event": "message.sent",
  "channel": "whatsapp",
  "provider": "vonage_whatsapp",
  "provider_message_id": "aaaaaaaa-bbbb-...",
  "status": "sent",
  "actor": "key_live_abcd1234",
  "duration_ms": 412
}
```

### 6.4 Logged fields by layer

| Layer | Fields |
| ----- | ------ |
| HTTP access | `request_id`, `method`, `path`, `status`, `duration_ms`, `client_ip`, `actor`, `channel` |
| AuthN/AuthZ | `event=auth.success/auth.failure/forbidden`, `actor`, `reason`, `scopes` |
| Orchestrator | `event=send.queued/sent/failed`, `group_id`, `message_id`, `channel`, `provider`, `error` |
| Providers | `event=sms.sent/whatsapp.sent/email.sent` + provider ids; **never** credentials or full body |
| Webhook | `event=webhook.delivery.delivered/failed`, redacted payload |

### 6.5 Secret-safe logging rules

1. **Never** log: `VONAGE_API_SECRET`, `AZURE_*_CONNECTION_STRING`/`accesskey`,
   `WHATSAPP_WEBHOOK_SECRET`, `AUTH_API_KEY`, raw `X-API-Key` header.
2. Apply `_redact()` (reuse `app/routers/webhooks.py::_redact`) to any dict being logged
   that may contain key-like fields (`token`, `secret`, `key`, `password`, `authorization`, ...).
3. Log key **presence** only, e.g. `vonage_api_key_loaded=true` (never the value).
4. Log a **hashed/truncated** actor id (`key_live_abcd…`) instead of full keys.
5. Message bodies are **not** logged at `INFO`; at `DEBUG` they may be logged for
   **email subject only** (no full bodies, no attachment contents).

### 6.6 Correlation IDs

- Generated per HTTP request (middleware): `request_id = req_<uuid4 hex>`.
- Propagated as `X-Request-ID` header on responses (echo if client supplied).
- Stored on every DB row: add `request_id` column (see Section 8.2).
- The orchestrator copies `request_id` and `group_id` into provider log records.

---

## 7. Audit Log Design

### 7.1 Purpose

An **append-only, tamper-evident, queryable** record of security-relevant events so that
operators can answer: *who did what, when, with which key, and what was the outcome.*

### 7.2 Audit events

| Category | Event | Fields (beyond base) |
| -------- | ----- | -------------------- |
| Authentication | `auth.success` | `actor`, `method=api_key` |
| | `auth.failure` | `actor` (if derivable), `reason=invalid_key/missing/expired/disabled` |
| Authorization | `authz.denied` | `actor`, `resource`, `required_scope`, `requested_channels` |
| Sends | `send.queued` | `group_id`, `channels[]`, `actor`, `reference` |
| | `send.sent` | `group_id`, `message_id`, `channel`, `provider`, `provider_message_id` |
| | `send.failed` | same + `error` |
| Status/reads | `status.read` | `message_id`/`group_id`, `actor` |
| Key management | `key.created` / `key.revoked` / `key.expired` | `key_id`, `actor` |
| Webhook | `webhook.auth_failed` | `reason`, `signature_present` |
| | `webhook.delivery_received` | `provider_message_id`, `status`, redacted error |
| Config | `config.changed` *(future)* | `setting`, `actor` |
| Startup | `app.started` | `version`, `mock_mode` |

### 7.3 Audit record schema

| Column | Type | Notes |
| ------ | ---- | ----- |
| `id` | INTEGER PK AUTOINCREMENT | monotonic sequence |
| `ts` | TEXT (ISO-8601 UTC) | event time |
| `event` | TEXT | `auth.success`, `send.sent`, … |
| `actor` | TEXT | `key_live_<id>` or `anonymous` or `webhook` |
| `request_id` | TEXT | correlation id |
| `group_id` | TEXT | nullable |
| `message_id` | TEXT | nullable |
| `channel` | TEXT | nullable |
| `provider` | TEXT | nullable |
| `provider_message_id` | TEXT | nullable |
| `status` | TEXT | nullable |
| `resource` | TEXT | endpoint/method |
| `detail` | TEXT | JSON-encoded, redacted extras |
| `severity` | TEXT | `info/warning/error` |
| `prev_hash` | TEXT | hash of previous row (chain) |
| `row_hash` | TEXT | hash of this row incl. `prev_hash` |

### 7.4 Tamper evidence (hash chaining)

Each row’s `row_hash = SHA-256(prev_hash | canonical(json(row fields without prev_hash/row_hash)))`.
- Recomputing the chain detects any past modification or deletion.
- The chain head is periodically anchored (e.g. logged + optionally shipped to storage).
- A verification command is provided: `python3 notification_service.py audit verify`.

### 7.5 Retention

- Config: `AUDIT_RETENTION_DAYS` (default `365`).
- Periodic cleanup (startup + daily background) deletes rows older than retention
  **only if** their `row_hash` chain is first sealed/exported, preserving evidence.
- Never delete without a configurable explicit `AUDIT_PURGE_ENABLED=true`.

### 7.6 What is NOT audited / logged

- Full notification message bodies (PII) — audit stores `message_id`, not content.
- Raw provider credentials.
- Attachment content.
- Email recipient lists in full for large groups (stores channel count only).

---

## 8. Data Model Changes

### 8.1 New table: `api_keys`

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,          -- live_<hex>
    key_hash     TEXT NOT NULL,             -- SHA-256 hex of secret
    name         TEXT,
    scopes       TEXT NOT NULL,             -- comma-separated
    enabled      INTEGER NOT NULL DEFAULT 1,
    expires_at   TEXT,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT
);
```

### 8.2 New column on `messages` (correlation + actor)

```sql
ALTER TABLE messages ADD COLUMN request_id TEXT;
ALTER TABLE messages ADD COLUMN created_by   TEXT;   -- key_id
```

Both additive; existing rows keep `NULL`. `init_db()` migration pattern already exists.

### 8.3 New table: `audit_logs`

Defined in Section 7.3.

---

## 9. Configuration Matrix

### 9.1 New environment variables (names only)

```
AUTH_ENABLED
AUTH_LEVEL            # none | api_key | keys
AUTH_API_KEY          # legacy single key (kept)
LOG_LEVEL
LOG_FORMAT            # json | text
LOG_FILE
LOG_REDACT_KEYS
WHATSAPP_WEBHOOK_SECRET
AUDIT_ENABLED
AUDIT_RETENTION_DAYS
AUDIT_PURGE_ENABLED
```

### 9.2 Backward compatibility

- `AUTH_ENABLED=false` behaves exactly as today (open access).
- `AUTH_ENABLED=true` **without** `AUTH_LEVEL` behaves exactly as today (`api_key` level,
  single `AUTH_API_KEY`).
- All existing request/response schemas and status codes remain unchanged.
- Webhook signature check is opt-in (`WHATSAPP_WEBHOOK_SECRET` unset ⇒ current behavior).
- `.env.example` gains only **placeholders**; no real secrets.

---

## 10. Component Placement

| Concern | Where it lives |
| ------- | -------------- |
| Identity resolution | `app/auth.py` (extended) — `require_api_key` + `require_scope` dependencies |
| Key management CLI | `notification_service.py` (`keys` subcommand) + `app/keys.py` |
| Request middleware (request_id, access log, redaction) | `app/middleware.py` (new) |
| Structured logger | `app/logging_config.py` (new) + `logging` stdlib |
| Audit writer + chain + verify | `app/audit.py` (new) |
| Audit persistence | `app/database.py` (extended: `audit_logs`, `api_keys`, migrations) |
| Channel authz checks | `app/orchestrator.py` (per-channel scope gate) |
| Webhook HMAC | `app/routers/webhooks.py` (signature dependency) |
| Config | `app/config.py` (new settings fields) |

### 10.1 Request flow (with authn/authz/logging/audit)

```
Client
  ↓ POST /api/v1/notifications/send + X-API-Key + X-Request-ID
Middleware (request_id, access log start)
  ↓
require_api_key  → resolve identity (audit: auth.success/failure)
  ↓
require_scope('send:whatsapp', ...) per requested channel
  ↓  (audit: authz.denied if insufficient)
Validation (schemas + validate_contact)
  ↓
Orchestrator → create_message(..., request_id, created_by)
  → audit: send.queued
  → background provider send
     → provider logs (structured, correlated)
     → audit: send.sent / send.failed
  → audit: send.complete
Response 202 (X-Request-ID echoed)
  ↓
Middleware writes access log (status, duration_ms)
```

---

## 11. Error Codes (additive)

| Code | HTTP | Meaning |
| ---- | ---- | ------- |
| `unauthorized` | 401 | missing/invalid API key (existing) |
| `key_expired` | 401 | key past `expires_at` |
| `key_disabled` | 401 | key revoked/disabled |
| `forbidden` | 403 | valid identity, insufficient scope |
| `webhook_signature_invalid` | 401 | bad HMAC on webhook payload |
| `server_config_error` | 500 | `AUTH_ENABLED=true` without usable key config (existing) |

---

## 12. Security Considerations

- **Constant-time comparisons** everywhere (`secrets.compare_digest`, `hmac.compare_digest`).
- **Hashing at rest** — raw key never stored; only SHA-256 digest.
- **Identical 401s** for all auth failures (no enumeration).
- **Redaction is centralized** so a single change covers all loggers.
- **Audit chaining** detects tampering; chain head anchoring for evidence.
- **Webhook HMAC** prevents forged delivery receipts from mutating message state.
- Rate limiting on `/api/v1/*` can be layered via middleware (documented, not implemented here).

---

## 13. Testing Implications

Mapped to `TEST_PLAN.md` (new/updated cases):

- AuthN: valid/missing/expired/disabled/revoked key; legacy single key; `AUTH_LEVEL=keys`.
- AuthZ: allowed/denied per channel scope; mixed-channel denial; `read:status` gating.
- Logging: JSON shape, redaction (secret never appears), request_id propagation.
- Audit: every event type written; chain verification detects tampering; retention/purge.
- Webhook HMAC: valid/invalid signature.
- All provider/API tests from `TEST_PLAN.md` continue to pass (no contract change).

---

## 14. Implementation Order

1. `app/logging_config.py` + middleware (request_id, access log, redaction).
2. `app/audit.py` + `audit_logs` table + migration.
3. Extend `app/auth.py` (`AUTH_LEVEL=keys`, key table, identity propagation) — keep legacy path.
4. `app/keys.py` + CLI `keys` subcommand (create/revoke/list/expire).
5. `require_scope(...)` + per-channel authorization in orchestrator.
6. Webhook HMAC signature dependency.
7. Audit events wired into authn/authz/orchestrator/webhook/key-mgmt.
8. `.env.example` + config fields + backward-compat checks.
9. Tests per `TEST_PLAN.md` (authn/authz/logging/audit sections) → 90% coverage.
10. Audit `verify` command + retention/purge + chain anchoring.

---

## 15. Definition of Done

- [ ] Every protected endpoint resolves an identity and enforces scopes.
- [ ] Failed auth attempts return identical 401s and are audited.
- [ ] No raw secrets or keys are ever logged or returned in responses.
- [ ] All operational logs are structured, correlated by `request_id`/`group_id`.
- [ ] All audit events in Section 7.2 are recorded with correct actor/outcome.
- [ ] Audit log is append-only with hash chaining; `audit verify` passes.
- [ ] Backward compatibility: `AUTH_ENABLED=false` and legacy single key behave unchanged.
- [ ] Existing API contract (status codes, schemas) is unchanged.
- [ ] All automated tests (existing + new) pass with >= 90% coverage.
- [ ] `.env.example` contains only placeholders; no secrets committed.
