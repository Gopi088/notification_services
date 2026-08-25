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
