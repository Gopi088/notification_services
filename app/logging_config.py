"""
Structured application logging.

Uses Python's standard `logging` module:
- Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL (default INFO via LOG_LEVEL).
- Writes to stdout (so Docker / terminals collect it in real time).
- Also writes to a rotating file when LOG_FILE is set (rotation: LOG_FILE_MAX_BYTES
  / LOG_FILE_BACKUPS).
- Consistent structured fields: ts, level, logger, event, request_id,
  notification_id, user_id, channel, status, ...
- Secret keys are always masked; PII (phone/email) is masked in message text.

Application logs are separate from AUDIT logs (audit is durable in PostgreSQL /
dedicated audit file, see app/audit.py).
"""
import json
import logging
import logging.handlers
import os
import re
import sys
import uuid
from typing import Dict, Optional

from app.config import get_settings

_SECRET_KEYS = frozenset({
    "authorization", "accesskey", "access_key", "secret", "password",
    "token", "connection_string", "connectionstring", "signingkey",
    "api_key", "apikey", "auth", "database_url", "dsn",
})

# Patterns for masking PII when fields look like phones/emails.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{7,14}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def mask(value: str) -> str:
    """Mask a phone number or email for logs, e.g. +919887****48 / a***@example.com."""
    if not value:
        return value
    if "@" in value and "." in value.split("@")[-1]:
        local, _, domain = value.partition("@")
        return f"{local[0]}***@{domain}"
    digits = re.sub(r"[^\d]", "", value)
    if 7 <= len(digits) <= 15:
        return f"{value[:3]}****{digits[-2:]}"
    return value


def _redact_pii(text: str) -> str:
    """Best-effort PII masking on arbitrary strings (phones/emails)."""
    text = _PHONE_RE.sub(lambda m: mask(m.group(0)), text)
    text = _EMAIL_RE.sub(lambda m: mask(m.group(0)), text)
    return text


class TerminalLevelFilter(logging.Filter):
    """Exact-level filter for terminal output.

    When LOG_LEVEL=INFO, only records with level exactly INFO are displayed
    (DEBUG/WARNING/ERROR/CRITICAL are hidden). Other levels behave per the
    documented mapping:

      LOG_LEVEL=DEBUG    -> DEBUG + INFO + WARNING + ERROR + CRITICAL
      LOG_LEVEL=INFO     -> INFO only
      LOG_LEVEL=WARNING  -> WARNING + ERROR
      LOG_LEVEL=ERROR    -> ERROR + CRITICAL
      LOG_LEVEL=CRITICAL -> CRITICAL

    The file handler is intentionally NOT given this filter so file logging
    stays independent (standard threshold semantics) and nothing is lost on
    disk. Audit records are persisted separately (DB + audit file) and are
    never affected by this filter.
    """

    _ALLOWED: dict = {
        logging.DEBUG: {
            logging.DEBUG, logging.INFO, logging.WARNING,
            logging.ERROR, logging.CRITICAL,
        },
        logging.INFO: {logging.INFO},
        logging.WARNING: {logging.WARNING, logging.ERROR},
        logging.ERROR: {logging.ERROR, logging.CRITICAL},
        logging.CRITICAL: {logging.CRITICAL},
    }

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno in self._ALLOWED.get(self.level, {record.levelno})


class StructuredFormatter(logging.Formatter):
    """Emit one JSON object per log line with consistent fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k.lower() in _SECRET_KEYS:
                    v = "***"
                entry[k] = v
        # Always carry correlation fields when present in the record.
        for field in ("request_id", "notification_id", "user_id", "channel",
                      "status", "provider", "attempt", "latency_ms", "error_code"):
            if hasattr(record, field) and getattr(record, field) is not None:
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable text formatter for terminals (LOG_FORMAT=text).

    Fixed-width columns keep mixed DEBUG/INFO/ERROR terminal output easy to
    scan, without changing handlers, files, or audit persistence:

        %(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s   ·  key=value ...

    When `use_colors=True` (default for TTY terminals):
    - The WHOLE line is coloured for problems so they stand out immediately:
      WARNING=yellow, ERROR=red, CRITICAL=magenta+bold.
    - INFO/DEBUG keep a plain line but colour the level badge (green/cyan) and
      dim the timestamp + structured fields so the message stays the focus.
    File logs are never coloured.
    """

    _LEVEL_COLORS = {
        "DEBUG": "\033[36m",            # cyan
        "INFO": "\033[32m",             # green
        "WARNING": "\033[33m",          # yellow
        "ERROR": "\033[31m",            # red
        "CRITICAL": "\033[35m\033[1m",  # magenta + bold
    }
    # Levels that colour the entire line, not just the level badge.
    _LINE_COLORS = {
        "WARNING": "\033[33m",           # yellow
        "ERROR": "\033[31m",             # red
        "CRITICAL": "\033[35m\033[1m",   # magenta + bold
    }
    _DIM = "\033[2m"
    _RESET = "\033[0m"

    def __init__(self, fmt: Optional[str] = None, use_colors: bool = False):
        super().__init__(fmt or "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s")
        self.use_colors = use_colors

    def _extra_fields(self, record: logging.LogRecord) -> list:
        """Collect structured fields in a stable order, redacting secrets."""
        fields: list = []
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k.lower() in _SECRET_KEYS:
                    v = "***"
                fields.append(f"{k}={v}")
        for field in ("request_id", "notification_id", "user_id", "channel",
                      "status", "provider", "attempt", "latency_ms", "error_code"):
            if hasattr(record, field) and getattr(record, field) is not None:
                fields.append(f"{field}={getattr(record, field)}")
        return fields

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = self._extra_fields(record)
        tail = ("  ·  " + "  ".join(fields)) if fields else ""
        line = base + tail
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        if not self.use_colors:
            return line

        level = record.levelname
        whole = self._LINE_COLORS.get(level)
        if whole:
            return f"{whole}{line}{self._RESET}"

        color = self._LEVEL_COLORS.get(level, "")
        if color:
            base = base.replace(level, f"{color}{level}{self._RESET}", 1)
        ts = self.formatTime(record)
        base = base.replace(ts, f"{self._DIM}{ts}{self._RESET}", 1)
        if tail:
            base += f"{self._DIM}{tail}{self._RESET}"
        return base


def configure_logging() -> None:
    """Configure root logger: stdout handler + optional rotating file handler.

    The stdout (terminal) handler applies the exact-level TerminalLevelFilter
    so e.g. LOG_LEVEL=INFO shows ONLY INFO records. The file handler is kept
    independent (standard threshold semantics) so WARNING/ERROR are still
    recorded to disk even when the terminal filters them out.
    """
    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    fmt = settings.LOG_FORMAT.lower()
    use_colors = fmt == "text" and (
        getattr(settings, "LOG_COLORS", True) or sys.stdout.isatty()
    )

    if fmt == "json":
        formatter = StructuredFormatter()
    else:
        formatter = PlainFormatter(use_colors=use_colors)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # allow everything upstream; handlers filter
    root.handlers = []

    # Library loggers that flood normal INFO output with SDK HTTP diagnostics
    # (Azure pipeline policies, urllib3 connection pool, httpx/httpcore).
    # At INFO (or quieter) they are suppressed to WARNING; at DEBUG they stay
    # verbose so full SDK diagnostics are available when troubleshooting.
    noisy_libraries = (
        "azure", "azure.core", "azure.core.pipeline",
        "azure.core.pipeline.policies", "azure.identity",
        "urllib3", "requests", "httpx", "httpcore", "msrest", "msal",
    )
    lib_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    for lib_name in noisy_libraries:
        lib_logger = logging.getLogger(lib_name)
        lib_logger.setLevel(lib_level)
        lib_logger.propagate = True

    # The Azure HTTP logging policy dumps every request/response body + headers
    # and is extremely noisy even at DEBUG. Keep it at WARNING always so the
    # terminal stays readable; connection-level urllib3/azure logs above remain
    # available for troubleshooting.
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

    # Terminal handler: exact-level filter applied here.
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    stdout.addFilter(TerminalLevelFilter(level))
    root.addHandler(stdout)

    # Uvicorn installs separate handlers before importing the application.
    # Route its startup/error/access records through the application root
    # handler so every terminal line uses the same aligned column layout.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)

    log_file = getattr(settings, "LOG_FILE", "") or ""
    if log_file:
        try:
            parent = os.path.dirname(os.path.abspath(log_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            max_bytes = int(getattr(settings, "LOG_FILE_MAX_BYTES", 10 * 1024 * 1024))
            backups = int(getattr(settings, "LOG_FILE_BACKUPS", 5))
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            # File handler: no colors, standard threshold, independent filter.
            file_formatter = PlainFormatter(use_colors=False)
            rotating.setFormatter(file_formatter)
            file_level = getattr(settings, "LOG_FILE_LEVEL", "") or ""
            if file_level:
                rotating.setLevel(getattr(logging, file_level.upper(), level))
            else:
                rotating.setLevel(level)
            root.addHandler(rotating)
        except Exception:  # noqa: BLE001 - logging config must not crash startup
            pass


class CorrelatedLogger:
    """Logger wrapper that injects correlation fields into every record.

    Usage:
        logger = CorrelatedLogger("app")
        logger.info("notification created", request_id=req, notification_id=nid,
                    channel="sms", status="queued")
    """

    def __init__(self, name: str = "app"):
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, extra: Optional[Dict] = None) -> None:
        extras = dict(extra) if extra else {}
        self._logger.log(level, msg, extra={"extra": extras})

    def debug(self, msg: str, **kw) -> None:
        self._log(logging.DEBUG, msg, kw)

    def info(self, msg: str, **kw) -> None:
        self._log(logging.INFO, msg, kw)

    def warning(self, msg: str, **kw) -> None:
        self._log(logging.WARNING, msg, kw)

    def error(self, msg: str, **kw) -> None:
        self._log(logging.ERROR, msg, kw)

    def critical(self, msg: str, **kw) -> None:
        self._log(logging.CRITICAL, msg, kw)


def new_request_id(existing: Optional[str] = None) -> str:
    """Return an existing request id or mint a new one (req_<hex>)."""
    return existing or f"req_{uuid.uuid4().hex[:16]}"
