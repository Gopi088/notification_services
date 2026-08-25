# 13 — Application Logging

## 13.1 Purpose

Application logs explain *what the system is doing* (operational diagnostics),
visible in the terminal and through Docker. They are distinct from audit logs
(durable business record).

## 13.2 Log Levels

| Level | Use |
| ----- | --- |
| DEBUG | detailed development diagnostics |
| INFO | normal lifecycle events |
| WARNING | recoverable problems (retry, deferral, throttling) |
| ERROR | failed operations |
| CRITICAL | serious application/system failures |

Do not log everything as ERROR.

## 13.3 Structured Format

Prefer key=value or JSON-structured lines with correlation fields:

```
INFO request_id=REQ_123 notification_id=MSG_123 user_id=USR_001 channel=whatsapp event=notification_created status=queued
```

Supported formats: `text` (dev) and `json` (structured) via `LOG_FORMAT`.

## 13.4 Required Lifecycle Log Events

### Startup
```
Application starting
Configuration loaded
Database connected
Redis connected
Queue connected
Worker started
```

### API
```
Request received
Authentication successful/failed
Authorization successful/failed
Validation successful/failed
Idempotency check
Duplicate detection
Notification created
```

### Queue
```
Message published
Message consumed
Message acknowledged
Message requeued
Message moved to DLQ
```

### Worker
```
Worker started
Worker received notification
Worker processing
Provider selected
Provider request
Provider response
Worker completed
Worker failed
```

### Retry
```
Retry scheduled
Retry attempt
Retry exhausted
```

### Scheduling
```
Notification scheduled
Notification deferred
Quiet hours active
Next allowed send time
```

### Audit
```
Audit event recorded
```

### Shutdown
```
Shutdown requested
Stopping new work
Finishing in-flight work
Connections closed
Application stopped
```

## 13.5 Correlation Fields

Every notification lifecycle log carries:

- `request_id`
- `notification_id`
- `user_id` (when known)
- `channel`
- `provider`
- `status`
- `attempt` (worker/retry)
- `latency_ms` (provider)

## 13.6 Never Log

- API secret, API key, password
- Authorization header, access/refresh token
- Database password, Redis password, queue credentials
- Full phone numbers / emails where avoidable → mask (`+919887****48`,
  `a***@example.com`)
- Full message bodies

## 13.7 Masking

Reuse `app/logging_config.py::mask` for PII in log strings. Central `_redact`
masks secret-like keys.

## 13.8 Example Log Lines

```
INFO  Application starting version=2.0.0 mock_mode=false storage=postgres queue=true
INFO  Request received method=send channels=whatsapp request_id=req_4f1a...
INFO  Notification created notification_id=MSG_123 channel=whatsapp status=queued
INFO  Queue publish channel=whatsapp notification_id=MSG_123 entry=1234-0
INFO  Worker received notification notification_id=MSG_123 attempt=1
INFO  Provider request completed notification_id=MSG_123 provider=vonage_whatsapp latency_ms=420
INFO  Notification status changed notification_id=MSG_123 from=processing to=submitted
WARN  Retry scheduled notification_id=MSG_123 attempt=2 delay_ms=5278
ERROR Provider request failed notification_id=MSG_123 channel=whatsapp provider=vonage_whatsapp attempt=1 retryable=true error_code=TIMEOUT
INFO  Shutdown requested stopping new work
```

## 13.9 Configuration

| Env | Default | Notes |
| --- | ------- | ----- |
| `LOG_LEVEL` | `INFO` | root level |
| `LOG_FORMAT` | `text` | `text` or `json` |
| `LOG_FILE` | (stderr) | optional file path |

## 13.10 Tests

Logging tests capture output during tests and verify:

- required events exist,
- correlation fields exist,
- secrets/passwords/tokens are absent.

See [`27-TEST-PLAN.md`](27-TEST-PLAN.md) and [`evals/observability.yaml`](evals/observability.yaml).
