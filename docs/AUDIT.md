# Audit

## 10.1 Purpose

Persist complete business/security audit records. Audit is **separate** from
application logs and is never filtered by `LOG_LEVEL`.

## 10.2 Audit Events

- `authentication_success`
- `authentication_failed`
- `authorization_denied`
- `notification_created`
- `notification_queued`
- `notification_processing`
- `notification_submitted`
- `notification_delivered`
- `notification_failed`
- `notification_read`
- `notification_acknowledged`
- `retry_scheduled`
- `retry_attempted`
- `retry_exhausted`
- `queue_failure`
- `worker_failure`
- `rate_limit_exceeded`
- `idempotency_duplicate`
- `provider_webhook_received`
- `provider_webhook_rejected`
- `user_response_received`

## 10.3 Audit Fields

- `timestamp`
- `request_id`
- `user_id`
- `notification_id`
- `group_id`
- `channel`
- `action`
- `old_status`
- `new_status`
- `provider`
- `provider_message_id`
- `result`
- `failure_category`
- `source`
- `database_backend`
- `correlation_id`

## 10.4 Storage

- PostgreSQL / SQLite `audit_logs` table (durable).
- Dedicated audit file `AUDIT_LOG_FILE` (JSON lines), independent of the app log.

## 10.5 Never Store

- API keys
- API secrets
- passwords
- DATABASE_URL
- authorization headers
- full message bodies

## 10.6 Viewing

```bash
python3 notification_service.py audit              # from DB
python3 notification_service.py audit --file       # from audit file
```
