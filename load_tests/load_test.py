#!/usr/bin/env python3
"""
Load test for the notification-service (safe, no real provider calls).

How it works
------------
- Connects to a running server (default http://127.0.0.1:8000).
- Assumes the server runs with MOCK_MODE=true so NOTHING is actually sent to
  Twilio/Azure/Vonage.
- Authenticates via POST /api/v1/auth/login (JWT) - auth is NOT bypassed.

Two modes:

1. Finite (default): fires `--requests` POST /api/v1/notifications/send
   requests with `--concurrency` workers, then prints throughput / latency /
   failure breakdown.

2. Continuous (`--continuous`): runs indefinitely until Ctrl+C. A fixed pool
   of `--concurrency` workers continuously send requests as previous ones
   finish, and live statistics are printed every 10 seconds. Ctrl+C stops the
   workers gracefully and prints a final summary.

Security
--------
- Never sends a real notification (MOCK_MODE=true short-circuits providers).
- Never disables JWT auth; it obtains a token from the login endpoint.
- Does not log tokens, secrets, or message content.
"""
import argparse
import concurrent.futures
import itertools
import json
import os
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional

DEFAULT_BASE = "http://127.0.0.1:8000"
_BODY_CAP = 400  # chars of a response body shown as a representative sample
_LIVE_INTERVAL_SECONDS = 10.0
_LATENCY_WINDOW = 20000  # bounded latency samples kept for p50/p95


def _post_json(base, path, payload, token=None, timeout=15):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def login(base, client_id, client_secret):
    status, body = _post_json(
        base, "/api/v1/auth/login",
        {"client_id": client_id, "client_secret": client_secret},
    )
    if status != 200:
        raise SystemExit(f"Login failed ({status}). Check AUTH_CLIENT_ID/AUTH_CLIENT_SECRET.")
    return body["access_token"]


def _safe_body(body: Any) -> str:
    """Return a short, safe (non-secret) representation of a response body."""
    if isinstance(body, (dict, list)):
        try:
            body = json.dumps(body)
        except (TypeError, ValueError):
            body = str(body)
    text = str(body).strip()
    if len(text) > _BODY_CAP:
        text = text[:_BODY_CAP] + "... (truncated)"
    return text


def one_send(base: str, token: str, i: int) -> Dict[str, Any]:
    """Send one request and capture success OR failure detail.

    Returns a dict with keys: status (int|None), ok (bool), body (str),
    error (str|None), error_type (str|None), latency_ms (float).
    """
    payload = {
        "channels": [
            # Unique recipient per request -> duplicate-window dedup never
            # interferes, and every request is dispatched independently.
            {"channel": "sms", "contact": f"+919800000{i:05d}"}
        ],
        "message": f"load-test {i}",
    }
    start = time.perf_counter()
    try:
        status, body = _post_json(base, "/api/v1/notifications/send", payload, token=token)
        result = {
            "status": status,
            "ok": status == 202,
            "body": _safe_body(body),
            "error": None,
            "error_type": None,
        }
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort body capture
            body_text = ""
        result = {
            "status": exc.code,
            "ok": False,
            "body": _safe_body(body_text),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    except Exception as exc:  # noqa: BLE001 - transport/connection failures
        result = {
            "status": None,
            "ok": False,
            "body": "",
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }
    result["latency_ms"] = (time.perf_counter() - start) * 1000
    return result


class Stats:
    """Thread-safe, bounded statistics for (continuous) load tests."""

    def __init__(self, latency_window: int = _LATENCY_WINDOW,
                 rps_window_seconds: float = 10.0):
        self._lock = threading.Lock()
        self._ok = 0
        self._errors = 0
        self._total = 0
        self._by_status: Dict[str, int] = {}
        self._latencies = deque(maxlen=latency_window)
        self._completions: deque = deque()  # completion timestamps (pruned by time)
        self._rps_window = rps_window_seconds
        self._started = time.monotonic()

    def record(self, result: Dict[str, Any]) -> None:
        """Record one completed request result (called from any worker thread)."""
        with self._lock:
            self._total += 1
            if result.get("ok"):
                self._ok += 1
            else:
                self._errors += 1
            key = str(result.get("status")) if result.get("status") is not None else "transport_error"
            self._by_status[key] = self._by_status.get(key, 0) + 1
            lat = result.get("latency_ms")
            if lat is not None:
                self._latencies.append(lat)
            # Completion timestamps are pruned both here and in snapshot() to
            # keep current-RPS computation memory-bounded.
            self._completions.append(time.monotonic())
            now = time.monotonic()
            cutoff = now - self._rps_window
            while self._completions and self._completions[0] < cutoff:
                self._completions.popleft()

    def snapshot(self) -> Dict[str, Any]:
        """Return a bounded, lock-safe snapshot of the current stats."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._rps_window
            while self._completions and self._completions[0] < cutoff:
                self._completions.popleft()
            current_rps = len(self._completions) / self._rps_window if self._rps_window else 0.0
            elapsed = now - self._started
            avg_rps = self._total / elapsed if elapsed else 0.0
            lats = list(self._latencies)
            total, ok, errors = self._total, self._ok, self._errors
            by_status = dict(self._by_status)
        p50 = statistics.median(lats) if lats else None
        p95 = sorted(lats)[int(len(lats) * 0.95) - 1] if lats else None
        return {
            "total": total,
            "ok": ok,
            "errors": errors,
            "by_status": by_status,
            "current_rps": current_rps,
            "avg_rps": avg_rps,
            "elapsed": elapsed,
            "p50": p50,
            "p95": p95,
        }


def print_live_stats(stats: Stats) -> None:
    """Print one live-statistics line from the current Stats snapshot."""
    s = stats.snapshot()
    p50 = f"{s['p50']:.0f}" if s["p50"] is not None else "-"
    p95 = f"{s['p95']:.0f}" if s["p95"] is not None else "-"
    print(
        f"[live] total={s['total']} ok={s['ok']} errors={s['errors']} "
        f"current_rps={s['current_rps']:.1f} avg_rps={s['avg_rps']:.1f} "
        f"p50={p50}ms p95={p95}ms"
    )


def _run_live_printer(stats: Stats, stop_event: threading.Event,
                      interval: float = _LIVE_INTERVAL_SECONDS) -> None:
    """Print live stats every `interval` seconds until stop is set."""
    while not stop_event.is_set():
        if stop_event.wait(interval):
            break
        print_live_stats(stats)


def _graceful_result(exc: BaseException) -> Dict[str, Any]:
    return {"status": None, "ok": False, "body": "",
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__, "latency_ms": 0.0}


def run_continuous(base: str, token: str, concurrency: int,
                   stop_event: Optional[threading.Event] = None,
                   sender: Callable[..., Dict[str, Any]] = one_send,
                   live_interval: float = _LIVE_INTERVAL_SECONDS) -> Stats:
    """Run a fixed pool of workers sending requests continuously until stop.

    Each completed request is recorded and immediately replaced, so exactly
    `concurrency` requests are in flight at any time. Ctrl+C (SIGINT) sets the
    stop event; workers are then cancelled and a final Stats snapshot returned.
    """
    if stop_event is None:
        stop_event = threading.Event()

        def _on_sigint(signum, frame):  # noqa: ANN001
            print("\nCaught Ctrl+C - stopping...")
            stop_event.set()

        signal.signal(signal.SIGINT, _on_sigint)

    stats = Stats()
    counter = itertools.count(1)
    live_thread = threading.Thread(
        target=_run_live_printer, args=(stats, stop_event, live_interval), daemon=True
    )
    live_thread.start()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = set()
        for _ in range(concurrency):
            futs.add(ex.submit(sender, base, token, next(counter)))

        while not stop_event.is_set() and futs:
            done, _pending = concurrent.futures.wait(
                futs, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in done:
                futs.discard(fut)
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001 - never drop a failure
                    result = _graceful_result(exc)
                stats.record(result)
                if not stop_event.is_set():
                    futs.add(ex.submit(sender, base, token, next(counter)))

        # Graceful stop: cancel any pending futures and let running ones finish
        # within a short window (a request may take up to its HTTP timeout).
        for fut in futs:
            fut.cancel()
        ex.shutdown(wait=True, cancel_futures=True)

    return stats


def summarize(results: List[Dict[str, Any]], total_requests: int, total_seconds: float) -> Dict[str, Any]:
    """Aggregate per-request results into a summary (finite mode)."""
    latencies = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
    ok = sum(1 for r in results if r.get("ok"))
    errors = len(results) - ok
    by_status: Dict[str, int] = {}
    representative: Dict[str, str] = {}
    for r in results:
        if r.get("ok"):
            continue
        key = str(r.get("status")) if r.get("status") is not None else "transport_error"
        by_status[key] = by_status.get(key, 0) + 1
        # Keep the first response body seen for each failure status.
        body = r.get("body") or r.get("error") or ""
        if key not in representative and body:
            representative[key] = body
    return {
        "requests": total_requests,
        "ok": ok,
        "errors": errors,
        "total_seconds": total_seconds,
        "throughput": total_requests / total_seconds if total_seconds else 0.0,
        "by_status": dict(sorted(by_status.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)),
        "representative": representative,
        "p50": statistics.median(latencies) if latencies else None,
        "p95": sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else None,
        "max": max(latencies) if latencies else None,
    }


def print_report(summary: Dict[str, Any]) -> None:
    """Print the results summary and per-status failure breakdown."""
    print("\n--- results ---")
    print(f"requests:      {summary['requests']}")
    print(f"ok (202):      {summary['ok']}")
    print(f"errors:        {summary['errors']}")
    print(f"total time:    {summary['total_seconds']:.2f}s")
    print(f"throughput:    {summary['throughput']:.1f} req/s")
    if summary["p50"] is not None:
        print(f"latency p50:   {summary['p50']:.1f} ms")
    if summary["p95"] is not None:
        print(f"latency p95:   {summary['p95']:.1f} ms")
    if summary["max"] is not None:
        print(f"latency max:   {summary['max']:.1f} ms")

    if summary["errors"]:
        print("\nfailure breakdown:")
        for status_key, count in summary["by_status"].items():
            label = "transport_error" if status_key == "transport_error" else f"HTTP {status_key}"
            print(f"{label}: {count}")
        print("\nrepresentative response bodies:")
        for status_key, body in summary["representative"].items():
            label = "transport_error" if status_key == "transport_error" else f"HTTP {status_key}"
            print(f"--- {label} ---")
            print(f"{body}")


def print_continuous_summary(stats: Stats) -> None:
    """Print a final summary for continuous mode from the Stats snapshot."""
    s = stats.snapshot()
    p50 = f"{s['p50']:.1f}" if s["p50"] is not None else "-"
    p95 = f"{s['p95']:.1f}" if s["p95"] is not None else "-"
    print("\n--- final summary ---")
    print(f"requests:      {s['total']}")
    print(f"ok (202):      {s['ok']}")
    print(f"errors:        {s['errors']}")
    print(f"total time:    {s['elapsed']:.2f}s")
    print(f"average RPS:   {s['avg_rps']:.1f}")
    print(f"latency p50:   {p50} ms")
    print(f"latency p95:   {p95} ms")
    if s["errors"]:
        print("failure breakdown:")
        for status_key, count in sorted(s["by_status"].items(),
                                        key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            label = "transport_error" if status_key == "transport_error" else f"HTTP {status_key}"
            print(f"{label}: {count}")


def main():
    parser = argparse.ArgumentParser(description="Safe load test (mock mode, no real sends)")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="Server base URL")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--requests", type=int, default=200,
                        help="Number of requests (finite mode; ignored with --continuous)")
    parser.add_argument("--continuous", action="store_true",
                        help="Run indefinitely until Ctrl+C")
    parser.add_argument("--client-id", default=None, help="AUTH_CLIENT_ID (default from .env)")
    parser.add_argument("--client-secret", default=None, help="AUTH_CLIENT_SECRET (default from .env)")
    args = parser.parse_args()

    # Load default credentials from the environment first, then the project
    # .env, so local testing is easy (env vars / .env / --client-* all work).
    client_id = args.client_id or os.environ.get("AUTH_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("AUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        try:
            with open(".env", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("AUTH_CLIENT_ID="):
                        client_id = client_id or line.split("=", 1)[1].strip().strip('"\'')
                    elif line.startswith("AUTH_CLIENT_SECRET="):
                        client_secret = client_secret or line.split("=", 1)[1].strip().strip('"\'')
        except FileNotFoundError:
            pass
    if not client_id or not client_secret:
        raise SystemExit("Missing AUTH_CLIENT_ID / AUTH_CLIENT_SECRET. Pass --client-id/--client-secret.")

    print(f"Logging in to {args.base_url} ...")
    token = login(args.base_url, client_id, client_secret)

    if args.continuous:
        print(f"Obtained JWT. Continuous mode: {args.concurrency} workers until Ctrl+C...")
        stats = run_continuous(args.base_url, token, args.concurrency)
        print_continuous_summary(stats)
        sys.exit(0 if stats.snapshot()["errors"] == 0 else 1)

    print(f"Obtained JWT. Running {args.requests} requests with {args.concurrency} workers...")
    started = time.perf_counter()
    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(one_send, args.base_url, token, i) for i in range(args.requests)]
        for fut in concurrent.futures.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - never drop a failure
                results.append(_graceful_result(exc))

    total_seconds = time.perf_counter() - started
    summary = summarize(results, args.requests, total_seconds)
    print_report(summary)
    sys.exit(0 if summary["errors"] == 0 else 1)


if __name__ == "__main__":
    main()
