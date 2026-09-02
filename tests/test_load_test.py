"""Tests for the load-test diagnostics (summarize / print_report / one_send)."""
import json
import socket
import threading
import time
import urllib.error
from unittest.mock import patch

from load_tests import load_test as lt


def test_ok_result():
    r = lt.one_send("http://x", "tok", 1)
    # Without a real server it will fail — that's expected; the point is that
    # the result dict has the correct shape.
    assert "status" in r
    assert "ok" in r
    assert "body" in r
    assert "error" in r
    assert "error_type" in r
    assert "latency_ms" in r


def test_summarize_all_ok():
    results = [
        {"status": 202, "ok": True, "pass": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None, "channel": "sms", "message_type": "short", "outcome": "queued"},
        {"status": 202, "ok": True, "pass": True, "latency_ms": 150.0, "body": "", "error": None, "error_type": None, "channel": "sms", "message_type": "short", "outcome": "queued"},
        {"status": 202, "ok": True, "pass": True, "latency_ms": 200.0, "body": "", "error": None, "error_type": None, "channel": "sms", "message_type": "short", "outcome": "queued"},
        {"status": 202, "ok": True, "pass": True, "latency_ms": 50.0, "body": "", "error": None, "error_type": None, "channel": "sms", "message_type": "short", "outcome": "queued"},
    ]
    s = lt.summarize(results, len(results), 2.0)
    assert s["requests"] == 4
    assert s["ok"] == 4
    assert s["errors"] == 0
    assert s["p50"] == 125.0  # median of 50, 100, 150, 200
    assert s["p95"] == 150.0  # index int(4*0.95)-1 = 2 -> 3rd value
    assert s["max_latency"] == 200.0
    assert s["by_status"] == {}
    assert s["representative"] == {}
    assert s["throughput"] == 2.0


def test_summarize_with_failures():
    results = [
        {"status": 202, "ok": True, "pass": True, "latency_ms": 100.0, "body": "ok", "error": None, "error_type": None},
        {"status": 429, "ok": False, "pass": False, "latency_ms": 50.0, "body": '{"detail":{"error":{"code":"rate_limited"}}}',
         "error": "HTTP Error 429", "error_type": "HTTPError"},
        {"status": 429, "ok": False, "pass": False, "latency_ms": 40.0, "body": '{"detail":{"error":{"code":"rate_limited"}}}',
         "error": "HTTP Error 429", "error_type": "HTTPError"},
        {"status": 500, "ok": False, "pass": False, "latency_ms": 30.0, "body": "{}",
         "error": "HTTP Error 500", "error_type": "HTTPError"},
        {"status": None, "ok": False, "pass": False, "latency_ms": 0.0, "body": "",
         "error": "ConnectionError: timeout", "error_type": "ConnectionError"},
    ]
    s = lt.summarize(results, len(results), 1.0)
    assert s["requests"] == 5
    assert s["ok"] == 1
    assert s["errors"] == 4
    assert s["by_status"]["429"] == 2
    assert s["by_status"]["500"] == 1
    assert s["by_status"]["transport_error"] == 1
    assert "429" in s["representative"]
    assert "500" in s["representative"]
    assert "transport_error" in s["representative"]


def test_safe_body_truncates():
    long = "x" * 1000
    capped = lt._safe_body(long)
    assert len(capped) == 400 + len("... (truncated)")


def test_safe_body_dict():
    d = {"detail": {"error": {"code": "rate_limited"}}}
    result = lt._safe_body(d)
    assert "rate_limited" in result
    assert len(result) < 100


def test_print_report_output(capsys):
    """print_report writes the expected sections to stdout."""
    summary = {
        "requests": 10,
        "ok": 8,
        "errors": 2,
        "total_seconds": 5.0,
        "throughput": 2.0,
        "by_status": {"429": 1, "500": 1},
        "by_outcome": {"http_failure": 2},
        "by_channel": {"sms": {"ok": 8, "fail": 2}},
        "by_message_type": {"short": 10},
        "error_breakdown": {"HTTPError": 2},
        "representative": {"429": '{"detail":{"error":{"code":"rate_limited"}}}', "500": "{}"},
        "p50": 100.0,
        "p95": 200.0,
        "p99": 300.0,
        "max_latency": 500.0,
        "auth_failures": 0,
        "auth_timeouts": 0,
        "token_refreshes": 0,
    }
    lt.print_report(summary)
    out = capsys.readouterr().out
    assert "ok:            8" in out
    assert "errors:        2" in out
    assert "outcomes:" in out

def test_stats_tracks_counts_and_latencies():
    """Stats.record/snapshot aggregate totals, statuses, and latency percentiles."""
    s = lt.Stats(latency_window=100)
    s.record({"status": 202, "ok": True, "pass": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None, "channel": "sms"})
    s.record({"status": 202, "ok": True, "pass": True, "latency_ms": 200.0, "body": "", "error": None, "error_type": None, "channel": "sms"})
    s.record({"status": 429, "ok": False, "pass": False, "latency_ms": 50.0, "body": "x", "error": "e", "error_type": "HTTPError", "channel": "sms"})
    snap = s.snapshot()
    assert snap["total"] == 3
    assert snap["ok"] == 2
    assert snap["errors"] == 1
    assert snap["by_status"]["202"] == 2
    assert snap["by_status"]["429"] == 1
    assert snap["p50"] == 100.0  # median of 50,100,200
    assert snap["p95"] == 100.0  # sorted[ int(3*0.95)-1 ] = sorted[1]


def test_stats_latency_window_is_bounded():
    """Stats keeps latency memory bounded (deque maxlen)."""
    s = lt.Stats(latency_window=10)
    for i in range(50):
        s.record({"status": 202, "ok": True, "pass": True, "latency_ms": float(i), "body": "", "error": None, "error_type": None, "channel": "sms"})
    snap = s.snapshot()
    # p50 over the last 10 latencies (40..49) -> median of even set = 44.5
    assert snap["p50"] == 44.5
    assert len(s._latencies) == 10


def test_stats_current_rps():
    """current_rps is based on completed requests in the window, not concurrency."""
    s = lt.Stats(rps_window_seconds=10.0)
    # Simulate 5 completions; current_rps within a 10s window < 1.0.
    for _ in range(5):
        s.record({"status": 202, "ok": True, "pass": True, "latency_ms": 1.0, "body": "", "error": None, "error_type": None, "channel": "sms"})
    snap = s.snapshot()
    assert snap["current_rps"] < 1.0
    assert snap["current_rps"] > 0.0


def test_run_continuous_stops_and_records():
    """run_continuous keeps sending until the stop event is set, then returns stats."""
    calls = {"n": 0}

    def fake_sender(base, token, i, channel="sms", worker="?", edge_case=None):
        calls["n"] += 1
        return {"status": 202, "ok": True, "pass": True, "latency_ms": 1.0, "body": "ok",
                "channel": channel, "worker": worker, "outcome": "queued",
                "error": None, "error_type": None}

    stop = threading.Event()
    # A thread sets stop after a moment so the loop terminates.
    def _stop_later():
        import time as _t
        _t.sleep(0.3)
        stop.set()

    threading.Thread(target=_stop_later, daemon=True).start()
    stats = lt.run_continuous("http://x", "tok", concurrency=4,
                              stop_event=stop, sender=fake_sender, live_interval=10.0)
    snap = stats.snapshot()
    assert snap["ok"] > 0
    assert snap["errors"] == 0
    # Fake sender returns ok; total requests must be at least concurrency.
    assert snap["total"] >= 4


def test_run_continuous_handles_errors():
    """Continuous mode tolerates connection/timeout errors without stopping."""
    calls = {"n": 0}

    def flaky_sender(base, token, i, channel="sms", worker="?", edge_case=None):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise TimeoutError("timed out")
        return {"status": 202, "ok": True, "pass": True, "latency_ms": 1.0, "body": "ok",
                "channel": channel, "worker": worker, "outcome": "queued",
                "error": None, "error_type": None}

    stop = threading.Event()

    def _stop_later():
        import time as _t
        _t.sleep(0.3)
        stop.set()

    threading.Thread(target=_stop_later, daemon=True).start()
    stats = lt.run_continuous("http://x", "tok", concurrency=4,
                              stop_event=stop, sender=flaky_sender, live_interval=10.0)
    snap = stats.snapshot()
    assert snap["total"] >= 4
    assert snap["errors"] > 0  # timeouts recorded, not fatal
    assert "transport_error" in snap["by_status"]


def test_print_continuous_summary(capsys):
    """print_continuous_summary prints the final totals + failure breakdown."""
    s = lt.Stats()
    s.record({"status": 202, "ok": True, "pass": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None, "channel": "sms", "message_type": "short", "outcome": "queued"})
    s.record({"status": 503, "ok": False, "pass": False, "latency_ms": 50.0, "body": "b", "error": "e", "error_type": "HTTPError", "channel": "sms", "message_type": "medium", "outcome": "http_failure"})
    lt.print_continuous_summary(s)
    out = capsys.readouterr().out
    assert "--- final summary ---" in out
    assert "ok:" in out and "1" in out
    assert "errors:" in out and "1" in out
    assert "outcomes:" in out
    assert "http_failure: 1" in out
    assert "per channel:" in out


def test_mask_secrets():
    from load_tests.load_test import _mask

    payload = {"client_secret": "secret123", "message": "hi", "channels": [{"channel": "sms"}]}
    masked = _mask(payload)
    assert masked["client_secret"] == "***"
    assert masked["message"] == "hi"
    assert masked["channels"][0]["channel"] == "sms"


def test_validate_pass():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    contract = CHANNEL_CONTRACTS["sms"]
    body = {"message_id": "m1", "status": "queued",
            "channels": [{"message_id": "c1", "channel": "sms", "status": "queued", "contact": "+9198000000001"}]}
    assert _validate(202, body, contract) == ""


def test_validate_bad_status():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    err = _validate(500, {}, CHANNEL_CONTRACTS["sms"])
    assert "expected status 202" in err


def test_validate_missing_fields():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    err = _validate(202, {"message_id": "m1"}, CHANNEL_CONTRACTS["sms"])
    assert "missing fields" in err


def test_validate_non_dict_body():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    err = _validate(202, None, CHANNEL_CONTRACTS["sms"])
    assert "not a dict" in err


def test_validate_wrong_channel():
    """A WhatsApp request that returns an sms channel must FAIL validation."""
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    contract = CHANNEL_CONTRACTS["whatsapp"]
    body = {"message_id": "m1", "status": "queued",
            "channels": [{"message_id": "c1", "channel": "sms", "status": "queued", "contact": "+9198000000001"}]}
    err = _validate(202, body, contract)
    assert "channels[0].channel=sms expected whatsapp" in err


def test_validate_empty_channels():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    contract = CHANNEL_CONTRACTS["sms"]
    err = _validate(202, {"message_id": "m1", "status": "queued", "channels": []}, contract)
    assert "non-empty" in err


def test_validate_email_channel():
    """Email contract expects channels[0].channel == email."""
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    contract = CHANNEL_CONTRACTS["email"]
    body = {"message_id": "m1", "status": "queued",
            "channels": [{"message_id": "c1", "channel": "email", "status": "queued",
                          "contact": "user+loadtest1@example.com"}]}
    assert _validate(202, body, contract) == ""


def test_recorder_verbose_output(capsys):
    from load_tests.load_test import Recorder

    r = Recorder(None, verbose=True)
    r.record({"pass": True, "ok": True, "status": 202, "latency_ms": 10.0, "request_num": 1,
              "request": {"channels": [{"channel": "sms"}]}, "body": "ok", "parsed": {},
              "validation_error": "", "error": None})
    out = capsys.readouterr().out
    assert "PASS #1" in out
    assert "#1" in out
    r.close()


def test_recorder_writes_log_file(tmp_path):
    from load_tests.load_test import Recorder

    logf = str(tmp_path / "test.jsonl")
    r = Recorder(logf, verbose=False)
    r.record({"pass": True, "ok": True, "status": 202, "latency_ms": 10.0, "request_num": 1,
              "request": {"channels": [{"channel": "sms"}]}, "body": "ok", "parsed": {},
              "validation_error": "", "error": None})
    r.close()
    import json
    with open(logf) as f:
        rec = json.loads(f.readline())
    assert rec["pass"] is True
    assert rec["actual_status"] == 202
    assert rec["expected_status"] == 202


def test_validate_accepts_delivered_status():
    """A delivered response is VALID (mock/dummy mode) - not hard-coded queued."""
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    body = {"message_id": "m1", "status": "delivered",
            "channels": [{"message_id": "c1", "channel": "sms", "status": "delivered", "contact": "+9198000000001"}],
            "duplicate": True}
    assert _validate(202, body, CHANNEL_CONTRACTS["sms"]) == ""
    assert _validate(202, body, CHANNEL_CONTRACTS["whatsapp"]) != ""  # wrong channel


def test_validate_rejects_bad_status_value():
    from load_tests.load_test import CHANNEL_CONTRACTS, _validate

    body = {"message_id": "m1", "status": "bogus",
            "channels": [{"message_id": "c1", "channel": "sms", "status": "bogus", "contact": "+9198000000001"}]}
    err = _validate(202, body, CHANNEL_CONTRACTS["sms"])
    assert "not a valid status" in err


def test_classify_outcomes():
    from load_tests.load_test import _classify

    assert _classify({"duplicate": True, "channels": [{"status": "delivered"}]}) == "duplicate"
    assert _classify({"duplicate": False, "channels": [{"status": "delivered"}]}) == "delivered"
    assert _classify({"duplicate": False, "channels": [{"status": "queued"}]}) == "queued"
    assert _classify({"channels": [{"status": "submitted"}]}) == "queued"


def test_validate_edge_correct_rejection():
    from load_tests.load_test import _validate_edge

    assert _validate_edge(422, 422) == ""
    assert "ACCEPTED" in _validate_edge(202, 422)
    assert "expected status 422" in _validate_edge(500, 422)


def test_parse_weights():
    from load_tests.load_test import _parse_weights

    w = _parse_weights("sms=50,whatsapp=30,email=20")
    assert w == {"sms": 50.0, "whatsapp": 30.0, "email": 20.0}
    # unknown channels ignored
    assert "telegram" not in _parse_weights("telegram=100,sms=1")


def test_build_valid_payload_unique():
    from load_tests.load_test import _build_request

    a, _ = _build_request(1, "sms")
    b, _ = _build_request(2, "sms")
    assert a["reference"] != b["reference"]
    assert a["channels"][0]["contact"] != b["channels"][0]["contact"]
    assert a["channels"][0]["channel"] == "sms"
    e, _ = _build_request(1, "email")
    assert "@" in e["channels"][0]["contact"]


def test_edge_case_payloads_are_invalid():
    from load_tests.load_test import EDGE_CASES

    for c in EDGE_CASES:
        payload = c["build"]("sms")
        assert c["expected_status"] in (400, 422)
        assert isinstance(payload, dict)


def test_recorder_display_modes():
    """Recorder display mode can be toggled at runtime."""
    from load_tests.load_test import (Recorder, DISPLAY_VERBOSE, DISPLAY_WARN,
                                      DISPLAY_ERRORS, DISPLAY_QUIET)

    r = Recorder(None)
    assert r.get_display() == DISPLAY_WARN  # default: WARN + ERROR
    r.set_display(DISPLAY_VERBOSE)
    assert r.get_display() == DISPLAY_VERBOSE
    r.set_display(DISPLAY_ERRORS)
    assert r.get_display() == DISPLAY_ERRORS
    r.set_display(DISPLAY_QUIET)
    assert r.get_display() == DISPLAY_QUIET
    r.close()


def test_recorder_verbose_prints_pass(capsys):
    from load_tests.load_test import Recorder, DISPLAY_VERBOSE

    r = Recorder(None)
    r.set_display(DISPLAY_VERBOSE)
    r.record({"pass": True, "ok": True, "status": 202, "latency_ms": 1, "request_num": 1,
              "channel": "sms", "worker": "w0", "outcome": "queued",
              "request": {"channels": [{"channel": "sms"}]}, "body": "ok", "parsed": {},
              "validation_error": "", "error": None})
    out = capsys.readouterr().out
    assert "PASS #1" in out
    r.close()


def test_recorder_warnings_show_warn(capsys):
    from load_tests.load_test import Recorder, DISPLAY_WARN

    r = Recorder(None)
    r.set_display(DISPLAY_WARN)
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 1, "request_num": 2,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": {}, "body": "err", "parsed": {},
              "validation_error": "", "error": None})
    out = capsys.readouterr().out
    assert "WARN #2" in out
    r.close()


def test_recorder_errors_only_suppresses_warn(capsys):
    """errors-only mode shows ERROR but NOT WARNING."""
    from load_tests.load_test import Recorder, DISPLAY_ERRORS

    r = Recorder(None)
    r.set_display(DISPLAY_ERRORS)
    # WARNING should be suppressed
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 1, "request_num": 3,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": {}, "body": "err", "parsed": {},
              "validation_error": "", "error": None})
    out = capsys.readouterr().out
    assert "WARN" not in out
    # ERROR should still show
    r.record({"pass": False, "ok": False, "status": 500, "latency_ms": 1, "request_num": 4,
              "channel": "sms", "worker": "w0", "outcome": "http_failure",
              "request": {}, "body": "err", "parsed": None,
              "validation_error": "err", "error": "boom"})
    out = capsys.readouterr().out
    assert "ERROR #4" in out
    r.close()


def test_random_message_types():
    """_random_message returns a message and a valid size-bucket label."""
    from load_tests.load_test import _random_message

    valid_types = {"empty", "short", "medium", "long", "very_long", "oversized"}
    seen = set()
    for _ in range(500):
        msg, label = _random_message("sms")
        assert label in valid_types
        assert isinstance(msg, str)
        if label == "empty":
            assert msg == ""
        elif label == "oversized":
            assert len(msg) > 1600  # SMS channel limit
        else:
            assert 1 <= len(msg) <= 1600
        seen.add(label)
    assert "medium" in seen and "long" in seen  # high-probability buckets


def test_oversized_message_is_edge_case():
    """Oversized messages are edge cases expected to be rejected."""
    from load_tests.load_test import EDGE_CASES, _SCHEMA_MESSAGE_CEILING

    oversized = [c for c in EDGE_CASES if c["name"] == "message_too_long"]
    assert oversized
    assert oversized[0]["expected_status"] == 422
    payload = oversized[0]["build"]("sms")
    assert len(payload["message"]) > _SCHEMA_MESSAGE_CEILING


def test_result_includes_message_length_and_type():
    """one_send records message_length and message_type on every request."""
    from load_tests.load_test import one_send

    r = one_send("http://x", "tok", 1, "sms", "w0", None)  # no real server
    assert "message_length" in r
    assert "message_type" in r
    assert r["message_type"] in {"empty", "short", "medium", "long", "very_long", "oversized"}


def test_recorder_log_includes_message_fields(tmp_path):
    from load_tests.load_test import Recorder

    logf = str(tmp_path / "msglen.jsonl")
    r = Recorder(logf, quiet=True)
    r.record({"pass": True, "ok": True, "status": 202, "latency_ms": 5.0, "request_num": 1,
              "channel": "sms", "worker": "w0", "outcome": "queued",
              "message_length": 42, "message_type": "medium",
              "request": {"channels": [{"channel": "sms", "contact": "+9198000000001"}]},
              "body": "ok", "parsed": {}, "validation_error": "", "error": None})
    r.close()
    import json
    with open(logf) as f:
        rec = json.loads(f.readline())
    assert rec["message_length"] == 42
    assert rec["message_type"] == "medium"
    assert rec["contact"] == "+9198000000001"


def test_recorder_handles_empty_channels(capsys):
    """Recorder must not crash when request.channels is [] (edge case)."""
    from load_tests.load_test import Recorder

    r = Recorder(None, quiet=True)
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 5.0, "request_num": 1,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": {"channels": [], "message": "x"},
              "body": "err", "parsed": {}, "validation_error": "", "error": None})
    r.close()  # would raise IndexError before the fix


def test_recorder_handles_missing_channels(capsys):
    """Recorder must not crash when request has no channels key."""
    from load_tests.load_test import Recorder

    r = Recorder(None, quiet=True)
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 5.0, "request_num": 2,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": {"message": "no channels"},
              "body": "err", "parsed": {}, "validation_error": "", "error": None})
    r.close()


def test_recorder_handles_empty_request(capsys):
    """Recorder must not crash when request is empty/missing."""
    from load_tests.load_test import Recorder

    r = Recorder(None, quiet=True)
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 5.0, "request_num": 3,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": None, "body": "err", "parsed": {},
              "validation_error": "", "error": None})
    r.close()


def test_safe_contact():
    from load_tests.load_test import _safe_contact

    assert _safe_contact({"channels": [{"channel": "sms", "contact": "+9198000000001"}]}) == "+9198000000001"
    assert _safe_contact({"channels": []}) is None
    assert _safe_contact({"message": "x"}) is None
    assert _safe_contact(None) is None


def test_recorder_jsonl_with_empty_channels(tmp_path):
    """Empty-channel edge case still writes a valid JSONL line."""
    from load_tests.load_test import Recorder

    logf = str(tmp_path / "empty.jsonl")
    r = Recorder(logf, quiet=True)
    r.record({"pass": True, "ok": True, "status": 422, "latency_ms": 5.0, "request_num": 4,
              "channel": "sms", "worker": "w0", "outcome": "expected_rejection",
              "request": {"channels": [], "message": "x"}, "message_length": 1,
              "message_type": "empty_channels",
              "body": "err", "parsed": {}, "validation_error": "", "error": None})
    r.close()
    import json
    with open(logf) as f:
        rec = json.loads(f.readline())
    assert rec["contact"] is None
    assert rec["message_type"] == "empty_channels"


def test_recorder_oversized_shows_concise_error(capsys):
    """Oversized (413) rejections print ONE concise ERROR line, not a body dump."""
    from load_tests.load_test import Recorder, DISPLAY_WARN

    r = Recorder(None, verbose=False, quiet=False)
    r.set_display(DISPLAY_WARN)
    r.record({
        "pass": True, "ok": False, "status": 422, "latency_ms": 25.0, "request_num": 57,
        "channel": "email", "worker": "w3", "outcome": "expected_rejection",
        "message_length": 5000, "message_type": "oversized", "configured_limit": 4096,
        "expected_status": 422, "expected_outcome": "rejected", "actual_outcome": "expected_rejection",
        "rejection_reason": "Message too large: length 5000 exceeds the limit of 4096 characters",
        "request": {"channels": [{"channel": "email"}], "message": "x" * 5000},
        "body": "err", "parsed": {}, "validation_error": "", "error": None,
    })
    out = capsys.readouterr().out
    # In warnings mode, expected rejections show a concise WARN line, not a body dump.
    assert "WARN #57" in out
    assert "len=5000" in out
    assert "REQUEST:" not in out
    assert "x" * 100 not in out
    r.close()


def test_oversized_edge_case_builds_over_limit():
    """The oversized edge case builds a message over the schema ceiling."""
    from load_tests.load_test import EDGE_CASES, _SCHEMA_MESSAGE_CEILING

    oversized = [c for c in EDGE_CASES if c["name"] == "message_too_long"][0]
    for ch in ("sms", "whatsapp", "email"):
        payload = oversized["build"](ch)
        assert len(payload["message"]) > _SCHEMA_MESSAGE_CEILING
        assert oversized["expected_status"] == 422


def test_validate_edge_413():
    """_validate_edge treats expected 413 as a correct rejection (not failure)."""
    from load_tests.load_test import _validate_edge

    assert _validate_edge(413, 413) == ""
    assert "ACCEPTED" in _validate_edge(202, 413)
    assert "expected status 413" in _validate_edge(422, 413)


def test_summary_reporter_writes_records(tmp_path):
    """SummaryReporter writes start/stats/error/final records to the summary file."""
    import json

    from load_tests.load_test import Stats, SummaryReporter

    path = str(tmp_path / "summary.log")
    rep = SummaryReporter(path)
    stats = Stats()
    stats.record({"pass": True, "status": 202, "channel": "sms", "outcome": "queued",
                  "latency_ms": 10.0, "message_type": "short"})
    stats.record({"pass": False, "status": 500, "channel": "sms", "outcome": "http_failure",
                  "latency_ms": 20.0, "message_type": "medium"})
    rep.snapshot(stats, {"request_num": 2})
    rep.error("unexpected_failure", "HTTP 500")
    rep.stop("ctrl_c", stats, {"request_num": 2})
    rep.close()

    with open(path) as f:
        events = [json.loads(line) for line in f]
    kinds = [e["event"] for e in events]
    assert "start" in kinds
    assert "stats" in kinds
    assert "error" in kinds
    assert "final" in kinds
    final = [e for e in events if e["event"] == "final"][0]
    assert final["requests"] == 2
    assert final["ok"] == 1
    assert final["expected_rejections"] == 0
    assert final["errors"] == 1
    assert final["stop_reason"] == "ctrl_c"
    assert final["by_channel"]["sms"]["ok"] == 1


def test_summary_reporter_counts_expected_rejections(tmp_path):
    import json

    from load_tests.load_test import Stats, SummaryReporter

    path = str(tmp_path / "summary2.log")
    rep = SummaryReporter(path)
    stats = Stats()
    stats.record({"pass": True, "status": 422, "channel": "sms", "outcome": "expected_rejection",
                  "latency_ms": 10.0, "message_type": "oversized"})
    rep.snapshot(stats)
    rep.stop("ctrl_c", stats)
    rep.close()
    with open(path) as f:
        events = [json.loads(line) for line in f]
    final = [e for e in events if e["event"] == "final"][0]
    assert final["expected_rejections"] == 1
    assert final["errors"] == 0  # expected rejection is NOT an error


# ---------------------------------------------------------------------------
# JWT refresh / auth-timeout behavior (tasks 1 & 2)
# ---------------------------------------------------------------------------

def _fake_token(exp_offset_minutes: float = 30) -> str:
    """Build a JWT-like token carrying an exp claim (not cryptographically valid)."""
    import base64
    import time as _t

    payload = {"sub": "u", "user_id": "u", "exp": _t.time() + exp_offset_minutes * 60}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{b64}.sig"


def test_token_refresher_parses_exp():
    from load_tests.load_test import TokenRefresher

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(30))
    assert tr._expires_at is not None
    assert tr._expires_at > time.time()
    assert TokenRefresher._parse_exp("") is None
    assert TokenRefresher._parse_exp("not-a-jwt") is None


def test_token_refresher_refresh_counts_and_logs():
    """refresh() performs a login, increments refresh_count and marks token."""
    from unittest.mock import patch

    from load_tests.load_test import TokenRefresher

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(30))
    with patch("load_tests.load_test.login", return_value=_fake_token(60)):
        tok = tr.refresh()
    assert tr.refresh_count == 1
    assert tr.failed is False
    assert tok.startswith("header.")


def test_token_refresher_records_auth_failure_and_marks_failed():
    """A failing refresh records the failure, increments auth_failures and sets failed."""
    from unittest.mock import patch

    from load_tests.load_test import TokenRefresher

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(30))
    with patch("load_tests.load_test.login", side_effect=RuntimeError("boom")):
        try:
            tr.refresh()
            assert False, "refresh should have raised"
        except RuntimeError:
            pass
    assert tr.failed is True
    assert tr.auth_failures == 1
    # Subsequent token() raises because auth is permanently failed.
    try:
        tr.token()
        assert False, "token() should raise once auth has failed"
    except RuntimeError:
        pass


def test_token_refresher_records_auth_timeout():
    """A timeout on login is classified as an auth timeout, not a generic failure."""
    from unittest.mock import patch

    from load_tests.load_test import TokenRefresher, _is_timeout_error

    import socket

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(30))
    err = socket.timeout("timed out")
    with patch("load_tests.load_test.login", side_effect=err):
        try:
            tr.refresh()
            assert False, "refresh should have raised"
        except socket.timeout:
            pass
    assert tr.failed is True
    assert tr.auth_timeouts == 1
    assert _is_timeout_error(err) is True


def test_one_send_401_refresh_and_retry_once():
    """A single 401 triggers one refresh and one retry; not counted as failure."""
    from unittest.mock import patch

    from load_tests.load_test import TokenRefresher, one_send

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(0.01))
    calls = {"n": 0}

    fixed_payload = {
        "channels": [{"channel": "sms", "contact": "+919800000001"}],
        "message": "hello load test", "reference": "ref-1",
    }

    def fake_post(base, path, payload, token=None, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(base + path, 401, "Unauthorized", {}, None)
        body = {"message_id": "m1", "status": "queued",
                "channels": [{"channel": payload["channels"][0]["channel"],
                              "message_id": "c1", "status": "queued",
                              "contact": payload["channels"][0]["contact"]}]}
        return 202, body, {}

    with patch("load_tests.load_test._post_json", side_effect=fake_post):
        with patch("load_tests.load_test.login", return_value=_fake_token(30)):
            with patch("load_tests.load_test._build_request", return_value=(fixed_payload, "short")):
                r = one_send("http://x", tr, 1, "sms", "w0", None)

    assert r["pass"] is True
    assert r["status"] == 202
    assert r["token_refreshed"] is True
    assert r["retry_count"] == 1
    assert calls["n"] == 2


def test_one_send_auth_failed_after_refresh():
    """401 on both attempts -> AUTH_FAILED, counted as auth failure."""
    from unittest.mock import patch

    from load_tests.load_test import TokenRefresher, one_send

    tr = TokenRefresher("http://x", "cid", "csec", initial_token=_fake_token(0.01))

    def fake_post(base, path, payload, token=None, timeout=15):
        raise urllib.error.HTTPError(base + path, 401, "Unauthorized", {}, None)

    with patch("load_tests.load_test._post_json", side_effect=fake_post):
        with patch("load_tests.load_test.login", return_value=_fake_token(30)):
            r = one_send("http://x", tr, 2, "whatsapp", "w0", None)

    assert r["auth_failed"] is True
    assert r["error_type"] == "AuthFailed"
    assert r["pass"] is False
    assert "authentication failed after token refresh" in r["error"]


def test_one_send_records_timeout_with_metadata():
    """A timeout on the send request is recorded with timeout_ms/upstream."""
    from unittest.mock import patch

    from load_tests.load_test import one_send

    def fake_post(base, path, payload, token=None, timeout=15):
        raise socket.timeout("timed out")

    with patch("load_tests.load_test._post_json", side_effect=fake_post):
        r = one_send("http://x", "tok", 3, "email", "w0", None)

    assert r["pass"] is False
    assert r["timeout"] is True
    assert r["timeout_ms"] is not None
    assert r["upstream"] == "send"
    assert r["error_type"] in ("TimeoutError", "OSError", "socket.timeout")


def test_stats_tracks_auth_and_refresh_counts():
    """Stats aggregates auth failures, timeouts and token refreshes."""
    s = lt.Stats()
    s.record({"pass": True, "status": 202, "channel": "sms", "outcome": "queued",
              "latency_ms": 1.0, "message_type": "short", "token_refreshed": True,
              "retry_count": 1, "timeout": False, "error_type": None})
    s.record({"pass": False, "status": 401, "channel": "sms", "outcome": "http_failure",
              "latency_ms": 1.0, "message_type": "short", "token_refreshed": False,
              "retry_count": 0, "timeout": False, "auth_failed": True,
              "auth_timeout": False, "error_type": "AuthFailed"})
    s.record({"pass": False, "status": None, "channel": "sms", "outcome": "transport_error",
              "latency_ms": 1.0, "message_type": "short", "token_refreshed": False,
              "retry_count": 0, "timeout": True, "timeout_ms": 1500.0,
              "upstream": "send", "error_type": "TimeoutError"})
    snap = s.snapshot()
    assert snap["auth_failures"] == 1
    assert snap["auth_timeouts"] == 0
    assert snap["token_refreshes"] == 1
    assert snap["retries"] == 1
    assert snap["timeouts"] == 1
    assert snap["error_breakdown"]["AuthFailed"] == 1
    assert snap["error_breakdown"]["TimeoutError"] == 1
    assert snap["max_latency"] == 1.0
    assert snap["start_time"] > 0
