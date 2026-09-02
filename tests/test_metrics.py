"""Tests for the lightweight performance metrics module."""
import time


def test_metrics_disabled_by_default():
    """record() is a no-op when metrics are disabled."""
    from app import metrics

    metrics.configure(False)
    metrics.clear()
    metrics.record("op", 1.0)
    assert metrics.snapshot() == {}


def test_metrics_record_and_snapshot(monkeypatch):
    """record() aggregates count/p50/p95/max/avg per operation."""
    from app import metrics

    metrics.configure(True)
    metrics.clear()
    for i in range(1, 101):
        metrics.record("sqlite_op", float(i))
    snap = metrics.snapshot()
    assert "sqlite_op" in snap
    agg = snap["sqlite_op"]
    assert agg["count"] == 100
    assert agg["p50"] == 51.0  # median of 1..100 = (50+51)/2
    assert agg["p95"] == 95.0
    assert agg["max"] == 100.0
    assert abs(agg["avg"] - 50.5) < 0.1
    metrics.configure(False)
    metrics.clear()


def test_metrics_bounded_memory(monkeypatch):
    """Metrics keep only the last _MAX_SAMPLES durations per operation."""
    from app import metrics

    metrics.configure(True)
    metrics.clear()
    for i in range(20000):
        metrics.record("op", 1.0)
    snap = metrics.snapshot()
    assert snap["op"]["count"] == 10000  # bounded
    metrics.configure(False)
    metrics.clear()


def test_metrics_timeit_context(monkeypatch):
    """timeit context manager records the block duration."""
    from app import metrics

    metrics.configure(True)
    metrics.clear()
    with metrics.timeit("block"):
        time.sleep(0.001)
    snap = metrics.snapshot()
    assert snap["block"]["count"] == 1
    assert snap["block"]["max"] >= 1.0
    metrics.configure(False)
    metrics.clear()


def test_metrics_timed_decorator(monkeypatch):
    """timed decorator records a function call duration."""
    from app import metrics

    metrics.configure(True)
    metrics.clear()

    @metrics.timed("fn")
    def fn():
        return 42

    assert fn() == 42
    snap = metrics.snapshot()
    assert snap["fn"]["count"] == 1
    metrics.configure(False)
    metrics.clear()


def test_metrics_endpoint_when_enabled(monkeypatch, client):
    """The performance metrics endpoint returns a snapshot when enabled."""
    from app import metrics
    from app.config import get_settings

    monkeypatch.setenv("PERFORMANCE_METRICS_ENABLED", "true")
    get_settings.cache_clear()
    metrics.configure(True)
    metrics.clear()
    metrics.record("sqlite_op", 5.0)

    r = client.get("/api/v1/performance/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["sqlite_op"]["count"] == 1
    assert body["process_pid"] > 0
    metrics.configure(False)
    metrics.clear()


def test_metrics_endpoint_when_disabled(monkeypatch, client):
    """The metrics endpoint returns an empty metrics object when disabled."""
    from app import metrics
    from app.config import get_settings

    monkeypatch.setenv("PERFORMANCE_METRICS_ENABLED", "false")
    get_settings.cache_clear()
    metrics.configure(False)
    metrics.clear()

    r = client.get("/api/v1/performance/metrics")
    assert r.status_code == 200
    assert r.json()["metrics"] == {}
