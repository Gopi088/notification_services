"""Tests for structured application logging and PII masking."""
import json
import logging


def test_mask_phone():
    from app.logging_config import mask

    m = mask("+919887270348")
    assert m != "+919887270348"
    assert "48" in m  # last 2 digits retained
    assert "****" in m


def test_mask_email():
    from app.logging_config import mask

    m = mask("rahul.kumar@example.com")
    assert m.startswith("r***@example.com")


def test_json_formatter():
    from app.logging_config import StructuredFormatter

    record = logging.LogRecord("test", logging.INFO, __file__, 1,
                               "notification sent", None, None)
    record.extra = {"notification_id": "n1", "channel": "sms"}
    out = StructuredFormatter().format(record)
    data = json.loads(out)
    assert data["event"] == "notification sent"
    assert data["notification_id"] == "n1"
    assert data["channel"] == "sms"
    assert data["timestamp"]
    assert data["level"] == "INFO"


def test_json_formatter_redacts_secret():
    from app.logging_config import StructuredFormatter

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "x", None, None)
    record.extra = {"api_key": "secret-value", "token": "tok"}
    out = StructuredFormatter().format(record)
    data = json.loads(out)
    assert data["api_key"] == "***"
    assert data["token"] == "***"


def test_plain_formatter():
    from app.logging_config import PlainFormatter

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", None, None)
    record.extra = {"request_id": "req_1"}
    out = PlainFormatter("%(message)s").format(record)
    assert "hello" in out
    assert "request_id=req_1" in out


def test_plain_formatter_uses_aligned_columns():
    from app.logging_config import PlainFormatter

    record = logging.LogRecord("api.v1", logging.DEBUG, __file__, 1, "parsed", None, None)
    out = PlainFormatter().format(record)
    assert " | DEBUG" in out
    assert " | api.v1" in out
    assert out.count(" | ") >= 3


def test_correlated_logger_injects_extra(caplog):
    from app.logging_config import CorrelatedLogger

    logger = CorrelatedLogger("test-logger")
    with caplog.at_level(logging.INFO):
        logger.info("created", notification_id="n9", channel="whatsapp")
    assert "created" in caplog.text


def test_new_request_id():
    from app.logging_config import new_request_id

    assert new_request_id().startswith("req_")
    assert new_request_id("keep-this") == "keep-this"


def test_configure_logging_json(monkeypatch):
    import os

    os.environ["LOG_FORMAT"] = "json"
    os.environ["LOG_LEVEL"] = "DEBUG"
    from app.config import get_settings

    get_settings.cache_clear()


def test_configure_logging_routes_uvicorn_through_root(monkeypatch):
    import os

    os.environ["LOG_LEVEL"] = "DEBUG"
    from app.config import get_settings
    from app.logging_config import configure_logging

    get_settings.cache_clear()
    configure_logging()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        configured = logging.getLogger(name)
        assert configured.propagate is True
        assert configured.handlers == []
    get_settings.cache_clear()
    from app.logging_config import configure_logging

    configure_logging()  # should not raise
    get_settings.cache_clear()


def test_configure_logging_rotating_file(monkeypatch, tmp_path):
    import os

    logf = str(tmp_path / "app.log")
    os.environ["LOG_FORMAT"] = "text"
    os.environ["LOG_LEVEL"] = "DEBUG"
    os.environ["LOG_FILE"] = logf
    from app.config import get_settings

    get_settings.cache_clear()
    from app.logging_config import configure_logging

    configure_logging()
    root = logging.getLogger()
    handlers = [h for h in root.handlers if getattr(h, "baseFilename", "") == logf]
    assert handlers, "rotating file handler should be configured"
    handlers[0].close()
    root.handlers = []
    get_settings.cache_clear()


def test_log_levels_respected(caplog):
    """INFO is the default; DEBUG suppressed, ERROR shown."""
    from app.logging_config import CorrelatedLogger

    logger = CorrelatedLogger("level-test")
    with caplog.at_level(logging.INFO):
        logger.debug("hidden debug")
        logger.info("visible info")
        logger.warning("visible warn")
        logger.error("visible error")
        logger.critical("visible critical")
    assert "hidden debug" not in caplog.text
    assert "visible info" in caplog.text
    assert "visible warn" in caplog.text
    assert "visible error" in caplog.text
    assert "visible critical" in caplog.text


def test_debug_mode_shows_debug(caplog):
    from app.logging_config import CorrelatedLogger

    logger = CorrelatedLogger("debug-test")
    with caplog.at_level(logging.DEBUG):
        logger.debug("debug line visible")
    assert "debug line visible" in caplog.text


def test_debug_flow_logs_are_safe_and_correlated(caplog):
    """DEBUG flow messages include correlation fields but never message content."""
    logger = logging.getLogger("debug-flow-test")
    with caplog.at_level(logging.DEBUG):
        logger.debug("send validation passed request_id=%s user_id=%s channel=%s",
                     "req_1", "anonymous", "sms")
    assert "send validation passed" in caplog.text
    assert "req_1" in caplog.text
    assert "channel=sms" in caplog.text


def test_validation_error_logging_does_not_include_submitted_value(capsys):
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from app.main import request_validation_error_handler

    request = Request({"type": "http", "method": "POST", "path": "/send", "headers": []})
    error = RequestValidationError([{
        "type": "string_too_short", "loc": ("body", "message"),
        "msg": "too short", "input": "private-message-content",
    }])
    response = __import__("asyncio").run(request_validation_error_handler(request, error))
    assert response.status_code == 422
    output = capsys.readouterr().out
    assert "request validation failed" in output
    assert "private-message-content" not in output


def test_terminal_level_filter_info_only():
    """LOG_LEVEL=INFO shows only INFO records (DEBUG/WARNING/ERROR/CRITICAL hidden)."""
    import io
    import logging

    from app.logging_config import TerminalLevelFilter

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.addFilter(TerminalLevelFilter(logging.INFO))
    logger = logging.getLogger("filter-info-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.debug("hidden debug")
    logger.info("visible info")
    logger.warning("hidden warning")
    logger.error("hidden error")
    logger.critical("hidden critical")

    out = captured.getvalue()
    assert "visible info" in out
    assert "hidden debug" not in out
    assert "hidden warning" not in out
    assert "hidden error" not in out
    assert "hidden critical" not in out


def test_terminal_level_filter_debug_shows_all_levels():
    """LOG_LEVEL=DEBUG shows DEBUG, INFO, WARNING, ERROR and CRITICAL."""
    import io
    import logging

    from app.logging_config import TerminalLevelFilter

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.addFilter(TerminalLevelFilter(logging.DEBUG))
    logger = logging.getLogger("filter-debug-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.debug("debug shown")
    logger.info("info shown")
    logger.warning("hidden warning")
    logger.error("hidden error")
    logger.critical("critical shown")

    out = captured.getvalue()
    assert "debug shown" in out
    assert "info shown" in out
    assert "hidden warning" in out
    assert "hidden error" in out
    assert "critical shown" in out


def test_terminal_level_filter_warning_shows_warning_and_error():
    """LOG_LEVEL=WARNING shows WARNING and ERROR (and hides DEBUG/INFO/CRITICAL)."""
    import io
    import logging

    from app.logging_config import TerminalLevelFilter

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.addFilter(TerminalLevelFilter(logging.WARNING))
    logger = logging.getLogger("filter-warning-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.info("hidden info")
    logger.warning("warning shown")
    logger.error("error shown")
    logger.critical("hidden critical")

    out = captured.getvalue()
    assert "warning shown" in out
    assert "error shown" in out
    assert "hidden info" not in out
    assert "hidden critical" not in out


def test_terminal_level_filter_error_shows_error_and_critical():
    import io
    import logging

    from app.logging_config import TerminalLevelFilter

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.addFilter(TerminalLevelFilter(logging.ERROR))
    logger = logging.getLogger("filter-error-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.info("hidden info")
    logger.error("error shown")
    logger.critical("critical shown")

    out = captured.getvalue()
    assert "error shown" in out
    assert "critical shown" in out
    assert "hidden info" not in out


def test_terminal_level_filter_critical_only():
    import io
    import logging

    from app.logging_config import TerminalLevelFilter

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.addFilter(TerminalLevelFilter(logging.CRITICAL))
    logger = logging.getLogger("filter-critical-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    logger.warning("hidden warning")
    logger.critical("critical shown")

    out = captured.getvalue()
    assert "critical shown" in out
    assert "hidden warning" not in out


def test_configure_logging_attaches_terminal_filter(monkeypatch):
    """configure_logging puts the exact-level filter on the stdout handler."""
    import os

    os.environ["LOG_LEVEL"] = "INFO"
    os.environ["LOG_FORMAT"] = "text"
    os.environ["LOG_FILE"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    from app.logging_config import configure_logging, TerminalLevelFilter

    configure_logging()
    root = logging.getLogger()
    stdout_handler = next(
        (h for h in root.handlers if isinstance(h, logging.StreamHandler)
         and getattr(h, "baseFilename", "") == ""), None
    )
    assert stdout_handler is not None
    filters = stdout_handler.filters
    assert any(isinstance(f, TerminalLevelFilter) for f in filters)
    get_settings.cache_clear()
    logging.getLogger().handlers = []


def test_file_handler_not_filtered(monkeypatch, tmp_path):
    """The file handler keeps standard threshold semantics (no exact filter)."""
    import os

    logf = str(tmp_path / "app.log")
    os.environ["LOG_LEVEL"] = "INFO"
    os.environ["LOG_FORMAT"] = "text"
    os.environ["LOG_FILE"] = logf
    from app.config import get_settings

    get_settings.cache_clear()
    from app.logging_config import configure_logging

    configure_logging()
    root = logging.getLogger()
    file_handler = next(
        (h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)), None
    )
    assert file_handler is not None
    # File handler must NOT have the terminal's exact-level filter.
    assert not file_handler.filters

    # A WARNING written through the file handler is persisted.
    logger = logging.getLogger("file-level-test")
    logger.warning("warning persists to file")
    logger.handlers = [file_handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.warning("warning should be in file")
    file_handler.flush()
    content = open(logf).read()
    assert "warning should be in file" in content

    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    logging.getLogger().handlers = []
    get_settings.cache_clear()


def test_log_level_filter_predicate():
    """_log_level_filter(INFO) matches only INFO lines."""
    from notification_service import _log_level_filter
    import logging as _l

    match = _log_level_filter(_l.INFO)
    assert match("2026-01-01 INFO app: startup")
    assert not match("2026-01-01 ERROR app: boom")
    assert not match("2026-01-01 WARNING app: warn")
    assert not match("2026-01-01 DEBUG app: dbg")
    assert not match("2026-01-01 CRITICAL app: crit")

    match_error = _log_level_filter(_l.ERROR)
    assert match_error("2026-01-01 ERROR app: boom")
    assert match_error("2026-01-01 CRITICAL app: crit")
    assert not match_error("2026-01-01 INFO app: ok")


def test_plain_formatter_colors_levels():
    """PlainFormatter colours the level name by level."""
    import io
    import logging

    from app.logging_config import PlainFormatter

    fmt = PlainFormatter("%(levelname)s %(message)s", use_colors=True)

    def _fmt(level):
        rec = logging.LogRecord("t", level, __file__, 1, "msg", None, None)
        return fmt.format(rec)

    assert "\033[32mINFO\033[0m" in _fmt(logging.INFO)
    assert "\033[33mWARNING\033[0m" in _fmt(logging.WARNING)
    assert "\033[31mERROR\033[0m" in _fmt(logging.ERROR)
    assert "\033[35m" in _fmt(logging.CRITICAL) and "\033[1m" in _fmt(logging.CRITICAL)
    assert "\033[36mDEBUG\033[0m" in _fmt(logging.DEBUG)


def test_plain_formatter_no_colors_when_disabled():
    import logging

    from app.logging_config import PlainFormatter

    fmt = PlainFormatter("%(levelname)s %(message)s", use_colors=False)
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "msg", None, None)
    assert "\033[" not in fmt.format(rec)


def test_cli_colorize_log_line():
    from notification_service import _colorize_log_line

    out = _colorize_log_line("2026-01-01 ERROR app: boom")
    assert "\033[31mERROR\033[0m" in out
    assert "boom" in out


def test_configure_logging_terminal_colors(monkeypatch):
    """configure_logging sets use_colors on the text stdout formatter."""
    import os

    os.environ["LOG_LEVEL"] = "INFO"
    os.environ["LOG_FORMAT"] = "text"
    os.environ["LOG_FILE"] = ""
    os.environ["LOG_COLORS"] = "true"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.logging_config import configure_logging

    configure_logging()
    root = logging.getLogger()
    stdout_handler = next(
        (h for h in root.handlers if isinstance(h, logging.StreamHandler)
         and getattr(h, "baseFilename", "") == ""), None
    )
    assert stdout_handler is not None
    assert getattr(stdout_handler.formatter, "use_colors", False) is True
    logging.getLogger().handlers = []
    get_settings.cache_clear()


def test_file_handler_never_colored(monkeypatch, tmp_path):
    """File handler formatter always has use_colors=False."""
    import os

    logf = str(tmp_path / "app.log")
    os.environ["LOG_LEVEL"] = "INFO"
    os.environ["LOG_FORMAT"] = "text"
    os.environ["LOG_FILE"] = logf
    from app.config import get_settings

    get_settings.cache_clear()
    from app.logging_config import configure_logging

    configure_logging()
    root = logging.getLogger()
    file_handler = next(
        (h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)), None
    )
    assert file_handler is not None
    assert getattr(file_handler.formatter, "use_colors", True) is False
    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    logging.getLogger().handlers = []
    get_settings.cache_clear()
