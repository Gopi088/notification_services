#!/usr/bin/env python3
"""
Production-style continuous endurance/load test for the notification service.

Runs continuously (until Ctrl+C or --duration), sending POST
/api/v1/notifications/send requests from a fixed worker pool, and records +
validates EVERY request against the expected API response. Detailed results
are written to a JSON-lines log file so the test can run unattended for days.

Usage:
    # terminal 1 - dummy provider (so no real provider is called)
    python3 load_tests/dummy_provider.py --port 9090

    # terminal 2 - notification service pointed at the dummy provider
    TWILIO_API_BASE_URL=http://127.0.0.1:9090 MOCK_MODE=false \
      AUTH_ENABLED=true JWT_SECRET_KEY=... \
      python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

    # terminal 3 - endurance test
    python3 load_tests/endurance_test.py --concurrency 20 \
      --log-file endurance_results.jsonl

Endurance test options:
    --concurrency        fixed worker pool (default 20)
    --rate               optional max requests/sec (default unlimited)
    --timeout            per-request HTTP timeout seconds (default 15)
    --duration           stop after N seconds (default: run until Ctrl+C)
    --fail-fast          abort on the first unexpected response
    --base-url           notification service base URL
    --log-file           path to write detailed JSON-lines results
    --client-id/--client-secret   JWT login credentials
    --dummy-latency      (informational; set on the dummy server itself)

Expected response for a send (202):
    { "message_id": "...", "channels": [ { "message_id": "...", "status": "queued", ... } ] }
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
from typing import Any, Dict, List, Optional

DEFAULT_BASE = "http://127.0.0.1:8000"
_LIVE_INTERVAL = 10.0
_MAX_SAMPLES = 20000  # bounded latency samples


# --------------------------------------------------------------------------- HTTP
def _post_json(base, path, payload, token=None, timeout=15):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def login(base, client_id, client_secret):
    status, body = _post_json(base, "/api/v1/auth/login",
                              {"client_id": client_id, "client_secret": client_secret})
    if status != 200:
        raise SystemExit(f"Login failed ({status}). Check AUTH_CLIENT_ID/AUTH_CLIENT_SECRET.")
    return body["access_token"]


def _mask(payload: Dict) -> Dict:
    """Return a copy of the payload with sensitive fields masked."""
    out = {}
    for k, v in payload.items():
        if k in ("client_secret", "password", "Authorization", "authorization"):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _mask(v)
        elif isinstance(v, list):
            out[k] = [_mask(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def _validate(expected_status: int, expected_fields: List[str], result: Dict) -> str:
    """Validate a result against the expected response. Returns '' on PASS."""
    if not result.get("ok"):
        return f"http_status={result.get('status')} expected={expected_status}"
    body = result.get("parsed")
    if not isinstance(body, dict):
        return f"response not an object: {result.get('body')}"
    missing = [f for f in expected_fields if f not in body]
    if missing:
        return f"missing fields={missing}"
    return ""


def _build_request(i: int, channel: str) -> Dict:
    return {
        "channels": [{"channel": channel, "contact": f"+9198{str(i).zfill(8)}"}],
        "message": f"endurance-{i}",
    }


# --------------------------------------------------------------------- recording
class Recorder:
    """Thread-safe recorder with bounded latency history and a JSON-lines file."""

    def __init__(self, log_path: Optional[str], timeout: float):
        self._lock = threading.Lock()
        self.ok = 0
        self.failures = 0
        self.total = 0
        self.fail_reasons: Dict[str, int] = {}
        self.latencies: deque = deque(maxlen=_MAX_SAMPLES)
        self.last_request: Optional[Dict] = None
        self.last_response: Optional[Dict] = None
        self._fh = None
        if log_path:
            self._fh = open(log_path, "a", encoding="utf-8")
        self.timeout = timeout
        self._started = time.time()

    def record(self, rec: Dict) -> None:
        with self._lock:
            self.total += 1
            if rec.get("pass"):
                self.ok += 1
            else:
                self.failures += 1
                reason = rec.get("error") or f"status={rec.get('actual_status')}"
                self.fail_reasons[reason] = self.fail_reasons.get(reason, 0) + 1
            lat = rec.get("latency_ms")
            if lat is not None:
                self.latencies.append(lat)
            self.last_request = rec.get("request")
            self.last_response = rec.get("actual_body")
            if self._fh:
                self._fh.write(json.dumps(rec, default=str) + "\n")
                self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()

    def snapshot(self) -> Dict:
        with self._lock:
            lat = list(self.latencies)
            elapsed = max(1e-6, time.time() - self._started)
            p50 = statistics.median(lat) if lat else None
            p95 = sorted(lat)[int(len(lat) * 0.95) - 1] if lat else None
            return {
                "total": self.total,
                "ok": self.ok,
                "failures": self.failures,
                "avg_rps": self.total / elapsed,
                "p50": p50,
                "p95": p95,
                "last_request": self.last_request,
                "last_response": self.last_response,
                "fail_reasons": dict(self.fail_reasons),
            }


class _PeriodicSummarizer(threading.Thread):
    """Print a live summary every interval seconds."""

    def __init__(self, recorder: "Recorder", interval: float):
        super().__init__(daemon=True)
        self.recorder = recorder
        self.interval = interval

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            s = self.recorder.snapshot()
            p50 = f"{s['p50']:.1f}" if s["p50"] is not None else "-"
            p95 = f"{s['p95']:.1f}" if s["p95"] is not None else "-"
            print(
                f"[live] rps={s['avg_rps']:.1f} total={s['total']} ok={s['ok']} "
                f"failed={s['failures']} p50={p50}ms p95={p95}ms"
            )


# ---------------------------------------------------------------------- worker
def one_request(base, token, i, channel, timeout):
    """Send one request and return a fully-recorded result dict."""
    request = _build_request(i, channel)
    start = time.perf_counter()
    rec = {
        "timestamp": time.time(),
        "request_id": str(uuid.uuid4()),
        "endpoint": "/api/v1/notifications/send",
        "channel": channel,
        "request": _mask(request),
        "expected_status": 202,
        "expected_fields": ["message_id", "channels"],
        "latency_ms": 0.0,
        "pass": False,
        "error": "",
    }
    try:
        status, body = _post_json(base, "/api/v1/notifications/send", request, token=token, timeout=timeout)
        rec["actual_status"] = status
        rec["actual_body"] = body
        rec["parsed"] = body if isinstance(body, dict) else None
        rec["ok"] = (status == 202)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        rec["actual_status"] = exc.code
        rec["actual_body"] = body
        rec["ok"] = False
    except Exception as exc:  # noqa: BLE001 - timeout/connection errors
        rec["actual_status"] = None
        rec["actual_body"] = f"{type(exc).__name__}: {exc}"
        rec["ok"] = False
    rec["latency_ms"] = (time.perf_counter() - start) * 1000
    # Validate against expected response.
    error = _validate(202, ["message_id", "channels"], rec)
    rec["error"] = error
    rec["pass"] = (not error) and rec.get("ok", False)
    return rec


def run(base, token, concurrency, timeout, duration, fail_fast, log_file, rate,
        channel="sms"):
    recorder = Recorder(log_file, timeout)
    summarizer = _PeriodicSummarizer(recorder, _LIVE_INTERVAL)
    summarizer.start()

    stop = threading.Event()
    if duration:
        threading.Timer(duration, stop.set).start()

    def _sigint(signum, frame):
        print("\nCaught Ctrl+C - stopping...")
        stop.set()

    signal.signal(signal.SIGINT, _sigint)

    counter = itertools.count(1)
    min_interval = (1.0 / rate) if rate and rate > 0 else 0.0

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = set()
        for _ in range(concurrency):
            futs.add(ex.submit(one_request, base, token, next(counter), channel, timeout))

        while not stop.is_set() and futs:
            if min_interval:
                time.sleep(min_interval)
            done, _pending = concurrent.futures.wait(
                futs, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in done:
                futs.discard(fut)
                try:
                    rec = fut.result()
                except Exception as exc:  # noqa: BLE001
                    rec = {"pass": False, "error": f"{type(exc).__name__}: {exc}",
                           "latency_ms": 0.0}
                recorder.record(rec)
                if fail_fast and not rec.get("pass", False):
                    print(f"FAIL-FAST triggered: {rec.get('error')}")
                    stop.set()
                    break
                if not stop.is_set():
                    futs.add(ex.submit(one_request, base, token, next(counter), channel, timeout))
        for fut in futs:
            fut.cancel()
        ex.shutdown(wait=True, cancel_futures=True)

    recorder.close()
    return recorder


def main():
    parser = argparse.ArgumentParser(description="Continuous endurance test (safe, no real sends)")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--rate", type=float, default=0.0, help="Max requests/sec (0=unlimited)")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--duration", type=float, default=0.0, help="Seconds to run (0=until Ctrl+C)")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--log-file", default="endurance_results.jsonl")
    parser.add_argument("--channel", default="sms", choices=["sms", "whatsapp", "email"])
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    args = parser.parse_args()

    client_id = args.client_id or os.environ.get("AUTH_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("AUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Missing AUTH_CLIENT_ID/AUTH_CLIENT_SECRET. Pass --client-id/--client-secret.")

    print(f"Logging in to {args.base_url} ...")
    token = login(args.base_url, client_id, client_secret)
    print(f"Start: concurrency={args.concurrency} channel={args.channel} "
          f"timeout={args.timeout}s rate={args.rate or 'unlimited'} fail_fast={args.fail_fast}")
    print(f"Log file: {args.log_file} (Ctrl+C to stop)\n")

    rec = run(args.base_url, token, args.concurrency, args.timeout,
              args.duration, args.fail_fast, args.log_file, args.rate, args.channel)
    s = rec.snapshot()
    print("\n--- final summary ---")
    print(f"total={s['total']} ok={s['ok']} failed={s['failures']}")
    print(f"avg_rps={s['avg_rps']:.1f} p50={s['p50']:.1f}ms p95={s['p95']:.1f}ms")
    if s["fail_reasons"]:
        print("failure breakdown:")
        for reason, count in s["fail_reasons"].items():
            print(f"  {reason}: {count}")
    sys.exit(0 if s["failures"] == 0 else 1)


if __name__ == "__main__":
    main()