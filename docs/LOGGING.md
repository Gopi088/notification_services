# Logging

## 9.1 Purpose

Structured application logging with request correlation. Terminal logging and
audit logging are separate; `LOG_LEVEL=INFO` controls terminal output without
deleting audit records.

## 9.2 Configuration

```env
LOG_LEVEL=INFO          # DEBUG/INFO/WARNING/ERROR/CRITICAL
LOG_FORMAT=text         # text or json
LOG_COLORS=true         # terminal ANSI colors (text format only)
LOG_FILE=logs/app.log   # optional rotating file
LOG_FILE_LEVEL=         # optional independent file level
```

## 9.3 Correlation Fields

Every request carries:

- `request_id`
- `user_id`
- `notification_id`
- `group_id`
- `channel`
- `operation`
- `status`
- `worker_id` (worker logs)

## 9.4 Logged Events

- application startup / shutdown
- database connection
- Redis connection
- authentication result
- API request received / completed
- validation result
- rate limit decision
- idempotency decision
- notification created / queued
- queue publish / consume
- worker started / processing
- provider request / response
- retry scheduled / attempted
- status transition
- webhook received
- notification delivered / failed
- acknowledgement
- exceptions

## 9.5 Terminal vs File vs Audit

| Stream | Purpose | Filtered? |
| ------ | ------- | --------- |
| Terminal (stdout) | operational logs | `TerminalLevelFilter` (INFO shows only INFO) |
| File (rotating) | durable operational logs | standard threshold (`LOG_FILE_LEVEL`) |
| Audit (DB + file) | business/security record | never filtered |

## 9.6 Secret Redaction

Never log: API keys, API secrets, passwords, DATABASE_URL, authorization
headers, message authentication tokens. Secret-like keys are masked to `***`.

## 9.7 Never Log

- `AUTH_API_KEY`
- `X-API-Key` values
- provider secrets / connection strings
- database passwords / DSNs
- full message bodies by default
