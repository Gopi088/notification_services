# ROADMAP.md — Building the Notification Service Further

This document captures **what is already built**, the **gap analysis** against the
target requirements, and a **phase-by-phase implementation plan** for:

1. Error handling (hardening)
2. Retry + Rate limiting
3. Authentication + Authorization
4. Logging + Audit records
5. Multi-threaded / multi-user request handling

---

## 1. Current state (what is done today)

| Area | Status | Where |
|---|---|---|
| Versioned REST API (`/api/v1` + legacy routes) | Done | `app/routers/v1.py`, `app/routers/notifications.py` |
| Uniform success/error envelope `{success, ..., error}` | Done for v1 only | `app/schemas.py::ErrorResponse` |
| Channel fan-out (whatsapp/sms/email) with group tracking | Done | `app/orchestrator.py` |
| Providers: Azure (SMS/Email/WhatsApp), Vonage (SMS/WhatsApp) + factory | Done | `app/providers/*` |
| SQLite persistence: `messages` table, `group_id`, status transitions | Done | `app/database.py` |
| Delivery-receipt webhook (Azure Event Grid) with secret redaction | Done | `app/routers/webhooks.py` |
| Contact validation (E.164 phone / email regex) | Done | `app/validation.py` |
| Email HTML templates with escaping + path-traversal protection | Done | `app/templates.py` |
| Basic auth: single shared static API key (`X-API-Key`) | Partial | `app/auth.py` |
| Basic error handling: global 500 handler, provider errors → `failed` + reason | Partial | `app/main.py`, `app/orchestrator.py::_safe_send` |
| Async dispatch of sends | Partial | `FastAPI BackgroundTasks` + ad-hoc `threading.Thread` (mock delivery only) |
| Logging | Partial | `logging.basicConfig(INFO)` in `main.py`, module loggers, webhook event logging |
| Retry | Not started | — |
| Rate limiting | Not started | — |
| Authorization (roles/scopes per caller) | Not started | — |
| Audit records | Not started | — |
| Structured/correlated logging (request IDs) | Not started | — |
| True concurrent worker system for many simultaneous users | Partial | sync endpoints run in uvicorn's threadpool; no worker pool, no queue, SQLite not tuned for concurrency |

---

## 2. Gap analysis

| Requirement | Gap |
|---|---|
| Error handling | Legacy routes return bare `{detail}` instead of the v1 envelope; Pydantic 422s don't use the envelope; provider errors are not classified (transient vs permanent); no stable machine-readable error-code catalogue |
| Retry | A single provider failure marks the message `failed` forever (`_safe_send`). No retries, no backoff, no attempt counter, no requeue |
| Rate limit | Any client can send unlimited requests; no per-key throttling, no 429 handling |
| Authentication | One global `AUTH_API_KEY` shared by everyone; no per-client identity, no key rotation/revocation, no hashing of keys at rest |
| Authorization | No concept of scopes/roles — any valid key can do everything |
| Logging & audit | Plain-text logs without correlation IDs; message bodies/contacts may hit logs; no tamper-evident record of *who did what* (audit trail); nothing persisted about API-level actions |
| Multi-threaded system | `BackgroundTasks` runs in-process after the response; a crash loses queued work; SQLite uses default journal mode (writer locks block readers under load); no bounded worker pool; no visibility into queue depth |

---

## 3. Phase plan

Suggested order: **Phase 1 → 2 → 6 (DB foundations) → 3 → 4 → 5**, since rate
limiting and audit both depend on knowing *which client* is calling
(Phase 3 identity), and the worker system needs the schema groundwork.

---

### Phase 1 — Error handling (hardening)

**Goal:** one predictable error shape everywhere, with a stable error-code
catalogue, and errors classified so later phases (retry) know what to do.

#### 1.1 Error taxonomy (`app/errors.py` — new)

```python
from enum import Enum

class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED     = "unauthorized"
    FORBIDDEN        = "forbidden"
    NOT_FOUND        = "not_found"
    RATE_LIMITED     = "rate_limited"
    PROVIDER_ERROR   = "provider_error"
    INTERNAL_ERROR   = "internal_error"

class AppError(Exception):
    def __init__(self, code, message, http_status=400, field=None): ...

class NotFoundError(AppError): ...      # 404
class UnauthorizedError(AppError): ...  # 401
class ForbiddenError(AppError): ...     # 403
class RateLimitedError(AppError): ...   # 429 (+ Retry-After)
```

Classify provider failures in `app/providers/base.py`:

```python
class ProviderTransientError(ProviderError):
    """Network timeouts, HTTP 429/5xx — safe to retry."""

class ProviderPermanentError(ProviderError):
    """Bad credentials, invalid recipient, rejected payload — never retry."""
```

Update Vonage/Azure providers to raise the right subclass (HTTP 401/403/400 →
permanent; timeout/connection/429/5xx → transient). Existing
`ProviderError` stays as the base class so current `except` blocks keep working.

#### 1.2 Global handlers in `app/main.py`

Register handlers that render the v1 envelope for **every** route:

```python
@app.exception_handler(AppError)
@app.exception_handler(RequestValidationError)   # turn 422 into the envelope
@app.exception_handler(StarletteHTTPException)   # covers abort() style errors
def ...(request, exc) -> JSONResponse: ...
```

- `RequestValidationError` → `400/422` with `error.code = "validation_error"` and
  `field` set from the first pydantic error location.
- Keep the catch-all `Exception → 500 {"detail": "Internal server error."}`
  but emit the envelope too and include the `request_id` (see Phase 5).

#### 1.3 Migrate legacy routes

Update `app/routers/notifications.py` so `/send` and `/status/{id}` return the
same envelope (keep old fields for compatibility, add `success`/`error` keys).

#### 1.4 Acceptance criteria

- [ ] Every endpoint returns `{success: false, error: {code, message, field}}` on failure.
- [ ] Error codes come only from `ErrorCode`.
- [ ] Provider failures are classified transient/permanent.
- [ ] All TEST_PLAN error cases still pass.

---

### Phase 2 — Retry with exponential backoff

**Goal:** transient provider failures are retried automatically before a
message is marked `failed`.

#### 2.1 Config (`app/config.py` + `.env.example`)

```python
RETRY_MAX_ATTEMPTS: int = 3          # total attempts incl. the first
RETRY_BACKOFF_BASE_SECONDS: float = 0.5
RETRY_BACKOFF_MAX_SECONDS: float = 30.0
```

#### 2.2 Schema migration (`app/database.py`)

Append to `_MIGRATIONS` (the existing PRAGMA-based migrator applies new columns
automatically):

```python
"ALTER TABLE messages ADD COLUMN attempt_count INTEGER DEFAULT 0",
"ALTER TABLE messages ADD COLUMN next_retry_at TEXT",
"ALTER TABLE messages ADD COLUMN last_attempt_at TEXT",
```

Add helpers: `increment_attempt(message_id)` and
`get_due_retries(now_iso) -> rows WHERE status='queued' AND next_retry_at <= ?`.

#### 2.3 Retry logic in `app/orchestrator.py::_safe_send`

```python
for attempt in range(1, settings.RETRY_MAX_ATTEMPTS + 1):
    try:
        result = fn()
        update_status(..., attempt_count=attempt)
        return
    except ProviderPermanentError as exc:
        update_status(message_id, status="failed", error=str(exc))
        return
    except ProviderTransientError as exc:
        if attempt == settings.RETRY_MAX_ATTEMPTS:
            update_status(message_id, status="failed", error=f"...after {attempt} attempts")
            return
        delay = min(base * 2 ** (attempt - 1) + random.uniform(0, 0.25), max_delay)
        update_status(message_id, status="queued", error=str(exc),
                      next_retry_at=(utcnow() + timedelta(seconds=delay)).isoformat())
        time.sleep(delay)   # replaced by the worker requeue in Phase 6
```

Rules:
- Only `ProviderTransientError` retries.
- Every attempt is persisted (`attempt_count`, `last_attempt_at`) and surfaced
  in the status endpoint.
- Sends must be **idempotent-safe**: store the `provider_message_id` after a
  successful call and never resend if one exists (guards against a timeout on a
  send that actually succeeded).
- Add `Retry-Attempted` info to the status response schema
  (`ChannelStatus.attempt_count`).

#### 2.4 Acceptance criteria

- [ ] Mocked transient failure → message eventually `sent` within `RETRY_MAX_ATTEMPTS`.
- [ ] Mocked permanent failure → immediately `failed`, exactly 1 attempt.
- [ ] Exhausted retries → `failed` with "failed after N attempts".
- [ ] Attempt history visible via `GET /api/v1/notifications/{id}/status`.

---

### Phase 3 — Rate limiting

**Goal:** protect providers and the service from abuse; enforce fair usage per
client (per API key once Phase 4 lands; per-IP until then).

#### 3.1 Approach: token bucket middleware (self-built, no new dependency)

New file `app/ratelimit.py`:

```python
class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float): ...
    def try_acquire(self) -> tuple[bool, float]: ...   # (allowed, retry_after)

class RateLimiter:
    """Buckets keyed by client id (API key hash or IP). Thread-safe (Lock)."""
```

Wire as an HTTP middleware (or a FastAPI dependency used alongside
`require_api_key`) applied to all `/api/v1/*` routes.

Exempt `/health` and the webhook route (provider callbacks must never be throttled).

#### 3.2 Config

```python
RATE_LIMIT_ENABLED: bool = True
RATE_LIMIT_CAPACITY: int = 30        # burst size
RATE_LIMIT_REFILL_PER_SECOND: float = 1.0
```

#### 3.3 Response contract

On limit exceeded return `429` with the standard envelope plus standard headers:

```json
{"success": false, "error": {"code": "rate_limited", "message": "Rate limit exceeded", "field": null}}
```

Headers: `Retry-After: <seconds>`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`.

#### 3.4 Limits & scale notes

- In-memory buckets are fine for the single-process deployment used here;
  document clearly that **multi-process / multi-host deployments need a shared
  store (Redis)** — add this as a future item, not now.
- Add `GET /api/v1/ratelimit` (authenticated) showing the caller's current
  usage — helpful for CLI users.

#### 3.5 Acceptance criteria

- [ ] Burst above capacity → 429 with `Retry-After`; envelope matches.
- [ ] Different clients get independent budgets.
- [ ] Webhook + health are never throttled.
- [ ] Load test script (e.g. `tests/load_ratelimit.py` using httpx) proves behaviour.

---

### Phase 4 — Authentication & Authorization

**Goal:** move from one shared secret to per-client identities with scoped
permissions and revocable keys.

#### 4.1 Keys at rest: new `api_keys` table

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,      -- public id, e.g. "ak_..."
    key_hash      TEXT NOT NULL UNIQUE,  -- SHA-256 hex of the raw key
    name          TEXT NOT NULL,         -- human label ("mobile-app-prod")
    scopes        TEXT NOT NULL,         -- comma-separated: "send,status"
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
);
```

- Raw keys look like `ak_live_<32 random urlsafe chars>`; shown **once** at
  creation, only the SHA-256 hash is stored.
- Lookup path: hash incoming `X-API-Key` → `secrets.compare_digest` against
  stored hashes → load identity.
- Keep `AUTH_API_KEY` working as a bootstrap "root" key (scope `admin`) so
  existing `.env` setups don't break.

#### 4.2 Authorization model: scopes

| Scope | Allows |
|---|---|
| `send` | `POST /notifications/send`, `POST /notifications/event` |
| `status` | `GET /notifications/*/status` |
| `admin` | key management endpoints + everything else |

Rework `app/auth.py`:

```python
def require_scopes(*needed: str):
    async def dependency(x_api_key: str = Header(default="")) -> ApiKeyIdentity:
        ...  # authenticate, then check scopes -> 401 / 403 (envelope)
    return dependency

# routers/v1.py
@router.post("/notifications/send", dependencies=[Depends(require_scopes("send"))])
@router.get("/notifications/{id}/status", dependencies=[Depends(require_scopes("status"))])
```

Return `403 forbidden` (new code from Phase 1) when authenticated but missing
the scope; keep `401 unauthorized` for bad/missing keys.

#### 4.3 Key-management endpoints (admin scope)

```
POST /api/v1/admin/keys            create key {name, scopes} -> raw key shown once
GET  /api/v1/admin/keys            list keys (id, name, scopes, active, last_used_at)
POST /api/v1/admin/keys/{id}/revoke
DELETE /api/v1/admin/keys/{id}
```

Audit every one of these actions (Phase 5).

#### 4.4 Future (explicitly out of scope for now)

- JWT/OAuth2 client-credentials flow, mTLS between internal services,
  per-key rate-limit tiers (tie Phase 3 buckets to `key_id`).

#### 4.5 Acceptance criteria

- [ ] Old single-key setups still work (bootstrap root key).
- [ ] Key without `send` scope calling send → `403 forbidden` envelope.
- [ ] Revoked/inactive key → `401`.
- [ ] Raw keys never logged, never returned again after creation, stored hashed.
- [ ] Brute-force resistance: constant-time compares; consider small delay on
      repeated failures per IP.

---

### Phase 5 — Logging & audit records

**Goal:** every request traceable end-to-end, secrets never logged, and a
persistent audit trail of security-relevant actions.

#### 5.1 Request correlation IDs

Middleware in `app/main.py` (or `app/middleware.py`):

- Generate `request_id` (UUID4) per request (honour incoming `X-Request-ID`).
- Attach to `contextvars` so all log lines in that request include it
  automatically; echo back as `X-Request-ID` response header; include it in the
  500 envelope body for supportability.

#### 5.2 Structured JSON logging

- Replace `logging.basicConfig(...)` with a JSON formatter
  (`{"ts", "level", "logger", "request_id", "event", ...}`) — hand-rolled or
  `python-json-logger` (one small dependency).
- Standardise event names: `request_received`, `send_accepted`,
  `dispatch_started`, `dispatch_result`, `retry_scheduled`, `webhook_update`,
  `auth_failure`, `rate_limited`, `audit`.
- Redaction helper (reuse the `_SECRET_KEYS` idea from `webhooks.py`) applied in
  the formatter; **never** log message bodies or full contact details — log
  `contact_hash` (SHA-256 prefix) instead.

#### 5.3 Audit trail: `audit_log` table

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    request_id   TEXT,
    actor_key_id TEXT,               -- from api_keys, NULL when auth disabled
    action       TEXT NOT NULL,       -- e.g. notification.send, key.created, key.revoked
    resource     TEXT,                -- message_id / group_id / target key_id
    outcome      TEXT NOT NULL,       -- success | denied | error
    detail       TEXT                 -- JSON blob (sanitised)
);
```

Write points (via `app/audit.py::record(action, ...)`, fire-and-forget safe):

- every authenticated send/event accepted (actor, channels count, reference)
- auth successes/failures and scope denials
- rate-limit hits
- key lifecycle events (create/revoke)
- webhook-driven status changes (who = "provider:<name>")

Expose read-only history: `GET /api/v1/admin/audit?action=&limit=` (scope
`admin`).

#### 5.4 Acceptance criteria

- [ ] Two concurrent requests produce interleaved-but-separable logs (request_id).
- [ ] Grep of logs for any raw API key / connection string returns nothing.
- [ ] Sending a message produces: `send_accepted` audit row + log events.
- [ ] Revoking a key produces an audit row visible via the admin endpoint.

---

### Phase 6 — Multi-threaded system for many simultaneous users

**Goal:** many clients sending at once must not drop or lose messages, must not
corrupt the DB, and queued work must survive process restarts.

#### 6.1 What already helps

- Sync FastAPI endpoints already execute in Starlette's threadpool → requests
  are naturally concurrent.
- Each request creates its own SQLite connection (`get_connection`).

#### 6.2 Make SQLite concurrency-safe (`app/database.py`)

```python
conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
conn.execute("PRAGMA busy_timeout=30000")
conn.execute("PRAGMA synchronous=NORMAL")
```

- Wrap writes in `BEGIN IMMEDIATE` transactions to avoid `SQLITE_BUSY`
  upgrade deadlocks; retry once on lock.
- Keep connections short-lived per operation (current design) — do **not**
  share one connection across threads.

#### 6.3 Durable outbox + worker pool (replace fire-and-forget dispatch)

Today a crash between "202 Accepted" and the `BackgroundTasks` execution loses
the send silently. Fix with an outbox pattern:

1. On accept: insert message rows with `status='queued'` **in the same
   transaction** (already the case) — the DB row *is* the job.
2. New `app/worker.py` starts at app startup (`on_startup`):

```python
executor = ThreadPoolExecutor(max_workers=settings.WORKER_THREADS)
stop_event = threading.Event()

def dispatcher_loop():
    while not stop_event.wait(poll_interval):        # e.g. 0.5s
        rows = claim_due_jobs(limit=BATCH)           # UPDATE ... SET status='processing'
                                                     # WHERE id IN (...) AND status='queued'
        for row in rows:
            executor.submit(deliver, row)
```

3. `deliver(row)` calls the provider path from Phase 2 (retries included);
   terminal states remain `sent` / `delivered` / `failed`.
4. Crash recovery: on startup, reset stale `processing` rows older than N
   minutes back to `queued` (at-least-once semantics; provider idempotency guard
   from Phase 2 prevents double-sends).

Config:

```python
WORKER_THREADS: int = 8
WORKER_POLL_INTERVAL_SECONDS: float = 0.5
WORKER_BATCH_SIZE: int = 32
STALE_PROCESSING_SECONDS: int = 300
```

Endpoints change from passing `BackgroundTasks` around to just enqueueing rows
(`orchestrate_send` drops its `background_tasks` argument).

#### 6.4 Observability

- `GET /api/v1/admin/stats` → queue depth, in-flight, sent/failed counts,
  worker utilisation (scope `admin`).
- Log `dispatch_wait_seconds` (created_at → dispatch_started) to prove SLAs
  under load.

#### 6.5 Scale-out notes (document, implement later)

- Single-process Uvicorn + thread pool comfortably handles the intended scale;
  for multi-process/multi-host, swap the polling dispatcher for Redis Streams /
  RQ / Celery and move rate-limit buckets + scheduler state to Redis (matches
  the Phase 3 note). Postgres replaces SQLite when write contention matters.

#### 6.6 Acceptance criteria

- [ ] Concurrency test: 50 parallel `curl`s / `httpx` posts → all 202s, all
      rows reach a terminal state, zero stuck `queued`.
- [ ] Kill the server mid-flight with pending rows → restart completes them
      (outbox proof).
- [ ] No `sqlite3.OperationalError: database is locked` under the load test.
- [ ] Status endpoint shows live progress for concurrent groups.

---

## 4. Config additions summary (`.env.example`)

```env
# --- Retry ---
RETRY_MAX_ATTEMPTS=3
RETRY_BACKOFF_BASE_SECONDS=0.5
RETRY_BACKOFF_MAX_SECONDS=30

# --- Rate limiting ---
RATE_LIMIT_ENABLED=true
RATE_LIMIT_CAPACITY=30
RATE_LIMIT_REFILL_PER_SECOND=1.0

# --- Auth ---
AUTH_ENABLED=true
AUTH_API_KEY=<bootstrap-admin-key>     # becomes the root admin key

# --- Workers ---
WORKER_THREADS=8
WORKER_POLL_INTERVAL_SECONDS=0.5
WORKER_BATCH_SIZE=32
STALE_PROCESSING_SECONDS=300
```

## 5. Schema migrations summary

| Migration | Table | Columns |
|---|---|---|
| M1 | `messages` | `attempt_count INTEGER DEFAULT 0`, `next_retry_at TEXT`, `last_attempt_at TEXT`, `status CHECK extended with 'processing'` |
| M2 | `api_keys` | new table (Phase 4) |
| M3 | `audit_log` | new table (Phase 5) |

Use the existing `_MIGRATIONS` + `PRAGMA table_info` pattern in
`app/database.py` — no external migration tool needed.

## 6. New dependencies

| Package | Phase | Purpose |
|---|---|---|
| *(none required)* | 2, 3, 6 | retry/backoff, token bucket, worker pool all stdlib |
| `python-json-logger` (optional) | 5 | JSON log formatting (hand-roll to avoid the dep if preferred) |
| `redis` (future only) | 3/6 scale-out | shared rate-limit + queue state across processes |

## 7. Testing additions (extend TEST_PLAN.md)

| Area | Cases |
|---|---|
| Retry (TC-R01+) | transient fails twice then succeeds → sent, attempts=3; permanent → failed, attempts=1; exhaustion message; idempotency (existing provider_message_id not resent) |
| Rate limit (TC-RL01+) | burst → 429 + Retry-After; independent buckets; health/webhook exempt; headers present |
| AuthZ (TC-AZ01+) | valid key wrong scope → 403; revoked key → 401; key creation shows raw key exactly once; hash-only storage |
| Audit (TC-AU01+) | send/key ops create audit rows; admin listing works; no PII/secrets in `detail` |
| Concurrency (TC-C01+) | 50 parallel sends all terminal; kill-restart recovery; zero `database is locked` |
| Errors (TC-E01+) | legacy routes return envelope; 422 → envelope; error codes from catalogue |

Load-test helper scripts belong in `tests/` (httpx-based, MOCK_MODE=true) so CI
stays offline.

## 8. Suggested milestone order

1. **M1 – Errors** (Phase 1): taxonomy + handlers + legacy envelope.
2. **M2 – Durability** (Phases 2 + 6.2): retries + WAL/busy_timeout.
3. **M3 – Workers** (Phase 6): outbox dispatcher + stats endpoint.
4. **M4 – Identity** (Phases 3 + 4): rate limiter keyed by identity + api_keys/scopes/admin.
5. **M5 – Observability** (Phase 5): request IDs, JSON logs, audit table + admin views.
6. **M6 – Hardening**: TEST_PLAN additions, coverage gate ≥ 90%, docs refresh.
