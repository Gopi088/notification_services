# Safe Local Load Testing

This guide explains how to run load tests against the notification service
**without sending a single real SMS/WhatsApp/Email**.

## Why it is safe

- The service runs with `MOCK_MODE=true`: every provider short-circuits and
  returns a mock result **before** calling Twilio, Azure, or Vonage. This is
  enforced in code (`if settings.MOCK_MODE: return mock...`) and proven by
  `tests/test_mock_mode.py`.
- JWT authentication stays **enabled** — the load test authenticates through
  the real `POST /api/v1/auth/login` endpoint and sends a Bearer token. No
  auth bypass.
- Each request uses a unique recipient, so the duplicate-window logic never
  suppresses a send and every request is processed independently.

## Start the server

```bash
# 1. Generate a JWT secret (once)
python3 -c "import secrets; print(secrets.token_hex(32))"

# 2. Start the server in MOCK_MODE with auth enabled
AUTH_ENABLED=true \
JWT_SECRET_KEY=<generated-secret> \
MOCK_MODE=true \
./run.sh
```

(You can also set the same values in `.env`: `MOCK_MODE=true`,
`AUTH_ENABLED=true`, `JWT_SECRET_KEY=<secret>`, and optionally
`AUTH_CLIENT_ID` / `AUTH_CLIENT_SECRET` for the login.)

You should see the startup banner with `mock_mode=True`, and the health
endpoint reports `auth_enabled: true`.

## Run the load test

```bash
# 50 workers, 200 requests
python3 load_tests/load_test.py --concurrency 50 --requests 200

# Custom server / auth credentials
python3 load_tests/load_test.py \
  --base-url http://127.0.0.1:8000 \
  --concurrency 100 --requests 1000 \
  --client-id dev-user --client-secret dev-password
```

The script:
1. Logs in (`POST /api/v1/auth/login`) and obtains a JWT.
2. Fires concurrent `POST /api/v1/notifications/send` requests with unique
   recipients.
3. Reports throughput (req/s), latency percentiles, and success count.

Every failed request is captured with its HTTP status code, response body,
exception type/message, and latency. The report then prints a per-status
`failure breakdown` (e.g. `HTTP 429: 40`) plus a short representative response
body for each failure type, so you can see exactly why requests failed instead
of just a count.

Because `MOCK_MODE=true`, nothing reaches Twilio/Azure/Vonage — the status
pipeline still runs (queued -> processing -> submitted/mock-delivered) exactly
as in production, which is what makes the load test representative.

## How to verify no real sends happened

- The startup log shows `mock_mode=True`.
- `tests/test_mock_mode.py` asserts the Twilio and Azure providers return mock
  results and **never** call the provider SDKs when `MOCK_MODE=true`:
  - `test_twilio_sms_mock_does_not_call_requests`
  - `test_twilio_whatsapp_mock_does_not_call_requests`
  - `test_azure_sms_mock_does_not_call_sdk`
  - `test_azure_email_mock_does_not_call_sdk`
  - `test_azure_whatsapp_mock_does_not_call_sdk`
- Run those tests any time before a load run:
  `python3 -m pytest tests/test_mock_mode.py -q`

## Notes

- Do not lower `AUTH_ENABLED` or weaken JWT auth for load testing; the script
  works with auth on.
- Rate limits still apply (per authenticated identity), matching production.
- For higher concurrency, the script uses a thread pool on the client side;
  the server queues work through its normal worker path.

## SQLite concurrency (local load testing)

The active storage layer (`app/storage.py`, used by `get_storage()`) configures
every SQLite connection for concurrency:

- `timeout=30.0` on `sqlite3.connect`
- `PRAGMA journal_mode=WAL` (set once on the startup connection; persistent
  file property)
- `PRAGMA busy_timeout=30000`
- `PRAGMA synchronous=NORMAL`

This prevents `database is locked` / `OperationalError` under concurrent load
(WAL allows one writer while readers keep working, and `busy_timeout` makes
contending writers wait instead of failing). The legacy `app/database.py`
connection helper uses the same settings for consistency.

To verify after the server starts:

```bash
sqlite3 notifications.db "PRAGMA journal_mode;"
# journal_mode = wal  (persistent file property)
```

Note: `busy_timeout` and `synchronous` are per-connection settings, so the
`sqlite3` CLI reports its own defaults (0 / 2) — the app applies them on every
connection, which `tests/test_storage.py` verifies.

For higher request rates than SQLite's single-writer ceiling, run more uvicorn
workers (`--workers N`) or use `STORAGE_BACKEND=postgres` in production.
