"""
Lightweight, per-process performance metrics for the notification service.

When PERFORMANCE_METRICS_ENABLED=true, instrumented operations record their
duration (ms) into bounded in-memory lists. A periodic logger thread prints
aggregated (p50/p95/max/avg/count) summaries every N seconds, and the
GET /api/v1/performance/metrics endpoint exposes the current snapshot.

Metrics are PER-PROCESS: each Uvicorn worker collects its own data. They are
never shared, exported, or persisted — purely for live load-test analysis.
"""
import collections
import contextlib
import functools
import logging
import threading
import time
from typing import Dict, Generator, List, Optional

logger = logging.getLogger("metrics")

_enabled = False
_store: Dict[str, List[float]] = {}
_lock = threading.Lock()
_MAX_SAMPLES = 10000  # keep last 10k durations per operation


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = enabled
    if enabled:
        logger.info("performance metrics enabled (per-process)")
    else:
        clear()


def record(name: str, duration_ms: float) -> None:
    """Record one duration sample for an operation (no-op when disabled)."""
    if not _enabled:
        return
    with _lock:
        samples = _store.get(name)
        if samples is None:
            samples = []
            _store[name] = samples
        samples.append(duration_ms)
        if len(samples) > _MAX_SAMPLES:
            _store[name] = samples[-_MAX_SAMPLES:]


@contextlib.contextmanager
def timeit(name: str) -> Generator[None, None, None]:
    """Context manager: record the duration of the enclosed block."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - start) * 1000)


def timed(name: str):
    """Decorator: record the duration of a sync function call (no-op when off)."""
    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            if not _enabled:
                return fn(*args, **kwargs)
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                record(name, (time.perf_counter() - start) * 1000)
        return _wrapper
    return _decorator


def snapshot() -> Dict[str, Dict[str, float]]:
    """Return a summary of every operation with samples.

    Each entry: count, p50, p95, max, avg (all ms).
    """
    result: Dict[str, Dict[str, float]] = {}
    with _lock:
        for name, samples in _store.items():
            if not samples:
                continue
            s = sorted(samples)
            n = len(s)
            result[name] = {
                "count": n,
                "p50": s[n // 2],
                "p95": s[int(n * 0.95) - 1] if n > 1 else s[0],
                "max": s[-1],
                "avg": sum(s) / n,
            }
    return result


def clear() -> None:
    with _lock:
        _store.clear()


def _periodic_logger(interval: float) -> None:
    """Log the aggregated metrics snapshot every `interval` seconds."""
    while True:
        time.sleep(interval)
        snap = snapshot()
        if not snap:
            continue
        for name, agg in snap.items():
            logger.info(
                "METRIC %s count=%d p50=%.1f p95=%.1f max=%.1f avg=%.1f",
                name, agg["count"], agg["p50"], agg["p95"], agg["max"], agg["avg"],
            )
        clear()


def start_periodic_logger(interval: float = 60.0) -> None:
    """Start a daemon thread that logs aggregated metrics periodically."""
    t = threading.Thread(target=_periodic_logger, args=(interval,), daemon=True)
    t.start()