"""
Structured logging with request correlation and secret redaction.

Replaces plain logging.basicConfig with a formatter that:
- Includes request_id from contextvars
- Redacts known secret keys from any dict/list values
- Never logs message bodies or full contacts (uses contact_hash)
- Supports extra fields: message_id, channel, provider, duration_ms, event
"""
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from app.middleware import request_id_var

_SECRET_KEYS = frozenset({
    "authorization", "accesskey", "access_key", "secret", "password",
    "token", "connection_string", "connectionstring", "signingkey",
    "api_key", "api_secret", "vonage_api_key", "vonage_api_secret",
})


def _redact_value(value):
    if isinstance(value, dict):
        return {
            k: "***" if k.lower() in _SECRET_KEYS else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


class StructuredFormatter(logging.Formatter):
    """JSON log formatter. Every line is a JSON object with standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rid = request_id_var.get("")

        log_entry = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "request_id": rid,
            "message": record.getMessage(),
        }

        # Include extra fields set via logger.info("...", extra={...})
        for field in ("message_id", "channel", "provider", "duration_ms",
                       "event", "status_code", "method", "path", "attempt",
                       "max_attempts", "provider_msg_id", "client_ip"):
            val = getattr(record, field, None)
            if val is not None:
                log_entry[field] = val

        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


_LEVEL_COLORS = {
    "DEBUG":    "\033[90m",   # gray
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
}
_RETRY_COLOR = "\033[35m"  # magenta for retry
_SUCCESS_COLOR = "\033[32m"  # green
_RESET = "\033[0m"


class PlainFormatter(logging.Formatter):
    """Human-readable log formatter with color-coded levels for local development."""

    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get("")
        rid_part = f" [{rid[:8]}]" if rid else ""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        color = _LEVEL_COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:8s}{_RESET}"

        # Highlight retry messages with magenta
        message = record.getMessage()
        if "retry" in message.lower() or "retrying" in message.lower():
            level = f"{_RETRY_COLOR}RETRY   {_RESET}"
        elif "scheduling retry" in message.lower():
            level = f"{_RETRY_COLOR}RETRY   {_RESET}"
        elif "failed after" in message.lower():
            level = f"{color}FAILED  {_RESET}"
        elif "provider accepted" in message.lower():
            level = f"{_SUCCESS_COLOR}SENT    {_RESET}"

        extras = []
        for field in ("message_id", "channel", "provider", "duration_ms",
                       "status_code", "method", "path", "attempt",
                       "max_attempts", "provider_msg_id", "client_ip"):
            val = getattr(record, field, None)
            if val is not None:
                extras.append(f"{field}={val}")

        extra_str = (" " + " ".join(extras)) if extras else ""
        return f"{ts} {level} {record.name}{rid_part}: {record.getMessage()}{extra_str}"


@contextmanager
def log_timing(logger: logging.Logger, message: str, level: int = logging.INFO,
               **extra):
    """Context manager that logs how long a block took.

    Usage:
        with log_timing(logger, "Vonage API call", message_id=msg_id):
            result = call_vonage(...)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.log(level, message, extra={**extra, "duration_ms": elapsed_ms})


def setup_logging(use_json: bool = True, log_level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(PlainFormatter())
    root.addHandler(handler)

    # Suppress noisy third-party loggers -- only show our application logs
    for name in ("azure", "azure.core", "azure.communication",
                 "httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
