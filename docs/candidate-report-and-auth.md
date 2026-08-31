# Candidate Report & JWT Authentication

## 1. Current authentication architecture

- `app/auth.py` provides `require_auth` (FastAPI dependency) and `user_id_from_request`.
- `app/main.py` mounts:
  - `app.include_router(auth_router)` — `POST /api/v1/auth/login` (public, issues JWTs)
  - `app.include_router(v1_router, dependencies=[Depends(require_auth)])`
  - `app.include_router(legacy_router, dependencies=[Depends(require_auth)])`
  - webhook routers (public, provider-validated): Azure Event Grid, Twilio signature, inbound
- `require_auth` reads `Authorization: Bearer <JWT>`, validates via PyJWT
  (`decode` with `JWT_SECRET_KEY`, `JWT_ALGORITHM`, required claims `sub`, `user_id`, `exp`).

## 2. Existing JWT bug / root cause

**Symptom:** `/api/v1/*` routes accepted requests without an Authorization header.

**Root cause:** `require_auth` starts with:

```python
if not settings.AUTH_ENABLED:
    return "anonymous"  # bypass
```

The default (`.env` and test `conftest.py`) is `AUTH_ENABLED=false`, so the
dependency short-circuits to `"anonymous"` and **no token is ever required**.
The JWT code existed and validated correctly, but the master switch disabled it.

**Fix:** keep `AUTH_ENABLED` as the explicit gate and document that production
**must** set `AUTH_ENABLED=true`. Add a startup warning when it is false (outside
mock mode). When `AUTH_ENABLED=true`:
- missing/invalid/expired/malformed token → `401`
- wrong signing secret → `401`
- valid token → allowed, `user_id` from claims drives audit/rate-limit/idempotency.

There is no per-route bypass: the v1 and legacy routers both carry the
dependency, and health/docs/openapi/webhooks are deliberately public.

## 3. New JWT authentication flow

1. Client calls `POST /api/v1/auth/login` with `{client_id, client_secret}`.
2. Server validates credentials (constant-time) and returns `{access_token, token_type: "bearer", expires_in, user_id}`.
3. Client sends `Authorization: Bearer <JWT>` on every `/api/v1/*` call.
4. `require_auth` decodes/validates the token (signature, expiration, algorithm,
   required claims) and returns the `user_id`.
5. The `user_id` is used for authorization scoping (candidate report), audit
   logs, idempotency keys and rate-limit buckets.

## 4. Protected endpoints

- `POST /api/v1/notifications/send`
- `POST /api/v1/notifications/event`
- `GET /api/v1/notifications/{id}/status`
- `GET /api/v1/reports/candidates/{candidate_id}` (new)
- Legacy `POST /send`, `GET /status/{id}`

## 5. Public / provider webhook endpoints

| Endpoint | Security |
| -------- | -------- |
| `POST /api/v1/auth/login` | credential validation, no JWT required |
| `/health`, `/api/v1/health*`, `/docs`, `/openapi.json` | public |
| `POST /api/v1/twilio/status` (+ aliases) | Twilio `X-Twilio-Signature` (HMAC-SHA1) |
| `POST /api/v1/whatsapp/webhook` (Azure) | Azure Event Grid validation |
| `POST /api/v1/inbound` | inbound reply webhook |

## 6. JWT configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `AUTH_ENABLED` | `false` | Master switch; **true in production** |
| `JWT_SECRET_KEY` | *(empty)* | Signing secret (`secrets.token_hex(32)`) |
| `JWT_ALGORITHM` | `HS256` | Signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Token lifetime |
| `AUTH_CLIENT_ID` / `AUTH_CLIENT_SECRET` | `notification-service` / *(empty)* | Local dev login credentials |

## 7. Candidate report API

`GET /api/v1/reports/candidates/{candidate_id}` (auth-protected)

A candidate is identified by their contact/recipient (the `notifications.recipient`
column — the project has no separate candidate model; the recipient IS the
candidate). No new reporting table: the report is computed from existing
notification rows.

Authorization scope: when authenticated, only rows where `created_by =
user_id` are included; in anonymous/dev mode all rows for the recipient are
returned.

### Example response

```json
{
  "candidate_id": "+919887270348",
  "total_messages": 3,
  "by_channel": { "sms": 2, "whatsapp": 1 },
  "by_status": { "delivered": 2, "submitted": 1 },
  "messages": [
    {
      "message_id": "…", "channel": "sms", "contact": "+919887270348",
      "status": "delivered", "provider": "twilio_sms",
      "provider_message_id": "SM…", "created_at": "…", "delivered_at": "…",
      "read_at": null, "retry_count": 0, "error": null,
      "group_id": "…", "reference": null, "resend_count": 0
    }
  ]
}
```

Query params: `limit` (default 50, max 100), `offset` (default 0).

## 8. HTTP error responses

- `401 Unauthorized` — missing/invalid/expired token, invalid login credentials
- `404 Not Found` — status/report target not found
- `500` — JWT_SECRET_KEY missing while `AUTH_ENABLED=true`

## 9. Local testing procedure

1. Generate a secret: `python -c "import secrets; print(secrets.token_hex(32))"`
2. In `.env`:
   ```
   AUTH_ENABLED=true
   JWT_SECRET_KEY=<generated>
   JWT_ALGORITHM=HS256
   JWT_ACCESS_TOKEN_EXPIRE_MINUTES=60
   AUTH_CLIENT_ID=dev-user
   AUTH_CLIENT_SECRET=dev-password
   ```
3. `./run.sh`
4. `curl -X POST http://127.0.0.1:8000/api/v1/auth/login -H "Content-Type: application/json" -d '{"client_id":"dev-user","client_secret":"dev-password"}'`
5. Use the returned `access_token`:
   `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/v1/health` and report.

## 10. Production / AWS configuration

Set the same env vars in the AWS task definition / Parameter Store
(`AUTH_ENABLED=true`, `JWT_SECRET_KEY` from Secrets Manager). No code changes,
no rebuild required (docker-compose passes them through).

## 11. Security considerations

- JWT secret and client secret come only from environment/config; never in source.
- JWTs, passwords, Authorization headers and message bodies are never logged.
- `hmac.compare_digest` / PyJWT constant-time verification used.
- Token expiry validated (`exp` claim).
- Webhook/provider auth is separate from client JWT auth.
- Report is scoped to the authenticated user's own records.
