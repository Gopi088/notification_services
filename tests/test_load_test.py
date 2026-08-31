"""Tests for the load-test diagnostics (summarize / print_report / one_send)."""
import json
import threading
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
        {"status": 202, "ok": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None},
        {"status": 202, "ok": True, "latency_ms": 150.0, "body": "", "error": None, "error_type": None},
        {"status": 202, "ok": True, "latency_ms": 200.0, "body": "", "error": None, "error_type": None},
        {"status": 202, "ok": True, "latency_ms": 50.0, "body": "", "error": None, "error_type": None},
    ]
    s = lt.summarize(results, len(results), 2.0)
    assert s["requests"] == 4
    assert s["ok"] == 4
    assert s["errors"] == 0
    assert s["p50"] == 125.0  # median of 50, 100, 150, 200
    assert s["p95"] == 150.0  # index int(4*0.95)-1 = 2 -> 3rd value
    assert s["max"] == 200.0
    assert s["by_status"] == {}
    assert s["representative"] == {}
    assert s["throughput"] == 2.0


def test_summarize_with_failures():
    results = [
        {"status": 202, "ok": True, "latency_ms": 100.0, "body": "ok", "error": None, "error_type": None},
        {"status": 429, "ok": False, "latency_ms": 50.0, "body": '{"detail":{"error":{"code":"rate_limited"}}}',
         "error": "HTTP Error 429", "error_type": "HTTPError"},
        {"status": 429, "ok": False, "latency_ms": 40.0, "body": '{"detail":{"error":{"code":"rate_limited"}}}',
         "error": "HTTP Error 429", "error_type": "HTTPError"},
        {"status": 500, "ok": False, "latency_ms": 30.0, "body": "{}",
         "error": "HTTP Error 500", "error_type": "HTTPError"},
        {"status": None, "ok": False, "latency_ms": 0.0, "body": "",
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
        "representative": {"429": '{"detail":{"error":{"code":"rate_limited"}}}', "500": "{}"},
        "p50": 100.0,
        "p95": 200.0,
        "max": 500.0,
    }
    lt.print_report(summary)
    out = capsys.readouterr().out
    assert "ok (202):      8" in out
    assert "errors:        2" in out
    assert "failure breakdown:" in out
    assert "HTTP 429: 1" in out
    assert "HTTP 500: 1" in out
    assert "representative response bodies:" in out
    assert "rate_limited" in out

def test_stats_tracks_counts_and_latencies():
    """Stats.record/snapshot aggregate totals, statuses, and latency percentiles."""
    s = lt.Stats(latency_window=100)
    s.record({"status": 202, "ok": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None})
    s.record({"status": 202, "ok": True, "latency_ms": 200.0, "body": "", "error": None, "error_type": None})
    s.record({"status": 429, "ok": False, "latency_ms": 50.0, "body": "x", "error": "e", "error_type": "HTTPError"})
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
        s.record({"status": 202, "ok": True, "latency_ms": float(i), "body": "", "error": None, "error_type": None})
    snap = s.snapshot()
    # p50 over the last 10 latencies (40..49) -> median of even set = 44.5
    assert snap["p50"] == 44.5
    assert len(s._latencies) == 10


def test_stats_current_rps():
    """current_rps is based on completed requests in the window, not concurrency."""
    s = lt.Stats(rps_window_seconds=10.0)
    # Simulate 5 completions; current_rps within a 10s window < 1.0.
    for _ in range(5):
        s.record({"status": 202, "ok": True, "latency_ms": 1.0, "body": "", "error": None, "error_type": None})
    snap = s.snapshot()
    assert snap["current_rps"] < 1.0
    assert snap["current_rps"] > 0.0


def test_run_continuous_stops_and_records():
    """run_continuous keeps sending until the stop event is set, then returns stats."""
    calls = {"n": 0}

    def fake_sender(base, token, i):
        calls["n"] += 1
        return {"status": 202, "ok": True, "latency_ms": 1.0, "body": "ok",
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

    def flaky_sender(base, token, i):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise TimeoutError("timed out")
        return {"status": 202, "ok": True, "latency_ms": 1.0, "body": "ok",
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
    s.record({"status": 202, "ok": True, "latency_ms": 100.0, "body": "", "error": None, "error_type": None})
    s.record({"status": 503, "ok": False, "latency_ms": 50.0, "body": "b", "error": "e", "error_type": "HTTPError"})
    lt.print_continuous_summary(s)
    out = capsys.readouterr().out
    assert "ok (202):      1" in out
    assert "errors:        1" in out
    assert "failure breakdown:" in out
    assert "HTTP 503: 1" in out
