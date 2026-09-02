#!/usr/bin/env python3
"""
Realistic randomized load test for the notification-service (safe, no real
provider calls).

Features
--------
- Randomly distributes requests across SMS, WhatsApp and Email using
  configurable weights (`--weights sms=50,whatsapp=30,email=20`).
- Randomizes valid contacts, messages (short/normal/long/near-max/boundary)
  and request data - no single hard-coded payload.
- Randomly injects edge-case (intentionally invalid/boundary) requests
  (`--edge-pct`) that have their own expected error contract; correct rejections
  are counted as expected, NOT server failures.
- Strict per-channel response contract validation based on the real API schema.
- Terminal shows only WARNING (edge expected-rejection) and RED ERROR (genuine
  failures) by default; `--verbose` also prints PASS lines; `--quiet` silences
  per-request lines. Periodic aggregate statistics (RPS, p50/p95/p99, per
  channel) always print.
- --continuous runs until Ctrl+C; logs are flushed per record and memory is
  bounded, so it is safe for multi-hour/day runs.

Security
--------
- Never sends a real notification (MOCK_MODE=true / dummy provider short-
  circuits real providers).
- Never disables JWT auth; it obtains a token from the login endpoint.
- Never logs tokens, secrets, or message content.
- Automatically refreshes the JWT before expiry and on HTTP 401; the original
  401 is retried once with the refreshed token and is NOT counted as a
  permanent failure if the retry succeeds.
"""
import argparse
import base64
import concurrent.futures
import itertools
import json
import os
import random
import signal
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Union

DEFAULT_BASE = "http://127.0.0.1:8000"
_BODY_CAP = 400  # chars of a response body shown as a representative sample
_LIVE_INTERVAL_SECONDS = 10.0
_LATENCY_WINDOW = 20000  # bounded latency samples kept for p50/p95/p99

# ANSI colors (disabled when not a TTY / --no-color)
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"
_USE_COLOR = True


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _USE_COLOR else text


def _green(t): return _c(_GREEN, t)
def _red(t): return _c(_RED, t)
def _yellow(t): return _c(_YELLOW, t)


# Per-channel message length limits (must match app/validation.py).
CHANNEL_MESSAGE_LIMITS = {
    "sms": 1600,
    "whatsapp": 4096,
    "email": 100000,
}
# The API schema's max_length ceiling (a single message cannot exceed this).
# Per-channel enforcement returns 413; the schema returns 422 for >1M.
_SCHEMA_MESSAGE_CEILING = 1000000

# All valid Status values the API can legitimately return in a SendResponse
# (see app/schemas.py Status enum). A send response may be queued/submitted/
# sent/delivered/read in mock/dummy mode, or any state for a duplicate replay.
VALID_STATUSES = {
    "created", "queued", "processing", "submitted", "retrying", "sent",
    "delivered", "read", "acknowledged", "failed", "dead_lettered",
    "cancelled", "scheduled", "expired", "partial",
}

# Per-channel expected response contracts (based on the real SendResponse
# schema: 202 with message_id + channels[] where each channel has
# message_id/channel/status/contact, plus an optional duplicate flag).
CHANNEL_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "sms": {"status": 202, "fields": ["message_id", "channels"], "channel": "sms"},
    "whatsapp": {"status": 202, "fields": ["message_id", "channels"], "channel": "whatsapp"},
    "email": {"status": 202, "fields": ["message_id", "channels"], "channel": "email"},
}
_EXPECTED_FIELDS = ["message_id", "channels"]  # legacy default


def _is_timeout_error(exc: BaseException) -> bool:
    """Detect timeout-related exceptions (urllib, socket, etc.)."""
    msg = str(exc).lower()
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, socket.timeout):
            return True
        if "timed out" in str(exc.reason).lower():
            return True
    if "timeout" in msg or "timed out" in msg:
        return True
    return False


def _post_json(base: str, path: str, payload: Dict, token: Optional[str] = None,
               timeout: float = 15) -> tuple:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
        resp_headers = dict(resp.headers)
        request_id = resp_headers.get("X-Request-ID") or resp_headers.get("X-Request-Id")
        return resp.status, body, {"request_id": request_id}


def login(base: str, client_id: str, client_secret: str) -> str:
    status, body, _ = _post_json(
        base, "/api/v1/auth/login",
        {"client_id": client_id, "client_secret": client_secret},
    )
    if status != 200:
        raise SystemExit(f"Login failed ({status}). Check AUTH_CLIENT_ID/AUTH_CLIENT_SECRET.")
    return body["access_token"]


class TokenRefresher:
    """Thread-safe JWT holder that logs in lazily, refreshes before expiry, and
    refreshes on HTTP 401 (called by one_send).

    Tracks the JWT `exp` claim and proactively obtains a fresh token before it
    expires so a long run does not generate a stream of 401s. A single lock
    prevents multiple workers refreshing simultaneously. On failure to obtain
    a fresh JWT, the reactor sets `self.failed = True` and records the error;
    callers must check `self.failed` before continuing.

    Counters (thread-safe via the same lock):
      refresh_count   - number of successful token refreshes
      auth_failures   - number of non-timeout refresh failures
      auth_timeouts   - number of timeout refresh failures
      failed          - True once a refresh attempt permanently fails
    """

    def __init__(self, base: str, client_id: str, client_secret: str,
                 initial_token: str = "", summary: Optional["SummaryReporter"] = None,
                 refresh_before_seconds: float = 60.0):
        self.base = base
        self.client_id = client_id
        self.client_secret = client_secret
        self.summary = summary
        self.refresh_before_seconds = refresh_before_seconds
        self._lock = threading.Lock()
        self._token = initial_token
        self._expires_at = self._parse_exp(initial_token)
        self.refresh_count = 0
        self.auth_failures = 0
        self.auth_timeouts = 0
        self.failed = False
        self.last_error = ""

    @staticmethod
    def _parse_exp(token: str) -> Optional[float]:
        """Return the unix expiry time from a JWT's exp claim, or None."""
        if not token:
            return None
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload_b64))
            return float(data["exp"])
        except Exception:  # noqa: BLE001 - malformed token
            return None

    def _log_refresh(self, reason: str) -> None:
        if self.summary:
            self.summary._write({
                "event": "auth_refresh",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
            })

    def _login_locked(self) -> str:
        """Obtain a fresh JWT. Detects timeouts and records them separately."""
        start = time.perf_counter()
        duration_ms = 0.0
        try:
            new = login(self.base, self.client_id, self.client_secret)
            duration_ms = (time.perf_counter() - start) * 1000
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            self.auth_timeouts += 1
            self.failed = True
            self.last_error = f"auth_timeout upstream=auth/login duration={duration_ms:.0f}ms error={exc}"
            if self.summary:
                self.summary.error("auth_timeout",
                                   f"upstream=auth/login duration={duration_ms:.0f}ms error={exc}")
            raise
        except Exception as exc:  # noqa: BLE001 - report + stop
            duration_ms = (time.perf_counter() - start) * 1000
            self.auth_failures += 1
            self.failed = True
            self.last_error = f"auth_failure: {exc}"
            if self.summary:
                self.summary.error("auth_refresh_failed",
                                   f"could not obtain a new JWT: {exc}")
            raise
        self.refresh_count += 1
        self._token = new
        self._expires_at = self._parse_exp(new)
        return new

    def token(self) -> str:
        """Return the current token, refreshing first if it is near expiry.

        Raises RuntimeError if auth has permanently failed.
        """
        if self.failed:
            raise RuntimeError(self.last_error or "auth permanently failed")
        with self._lock:
            if not self._token:
                self._login_locked()
            elif self._expires_at and time.time() >= self._expires_at - self.refresh_before_seconds:
                self._login_locked()
                self._log_refresh("token near/at expiry - proactive refresh")
            return self._token

    def refresh(self) -> str:
        """Obtain a fresh JWT immediately. Raises on failure."""
        if self.failed:
            raise RuntimeError(self.last_error or "auth permanently failed")
        with self._lock:
            self._login_locked()
            self._log_refresh("HTTP 401 - token expired or invalid")
            return self._token


def _safe_contact(request: Optional[Dict]) -> Optional[str]:
    """Safely extract the first channel's contact from a request payload."""
    if not request:
        return None
    channels = request.get("channels")
    if isinstance(channels, list) and channels and isinstance(channels[0], dict):
        return channels[0].get("contact")
    return None


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


def _mask(payload: Dict) -> Dict:
    """Return a copy of the payload with secrets masked."""
    out: Dict = {}
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


# ----------------------------------------------------------------- random data
_WORDS = ["hello", "world", "notification", "test", "load", "endurance", "alert",
          "order", "update", "sms", "whatsapp", "email", "reservation", "otp",
          "welcome", "reminder", "confirmation", "shipment", "invoice"]


def _random_phone() -> str:
    return "+91" + str(random.randint(6000000000, 9999999999))


def _random_email() -> str:
    return f"user{random.randint(1, 10**8)}@example.com"


def _random_contact(channel: str) -> str:
    return _random_email() if channel == "email" else _random_phone()


def _channel_limit(channel: str) -> int:
    """Return the per-channel message length limit."""
    return CHANNEL_MESSAGE_LIMITS.get(channel, 4096)


def _random_message(channel: str) -> tuple:
    """Generate a message with a randomized size bucket.

    Uses the channel's configured limit. Messages exceeding the channel limit
    are expected to be rejected (HTTP 413 or 422 depending on whether the
    schema cap is also exceeded).

    Returns (message_text, type_label) where type_label is one of:
    empty, short, medium, long, very_long, oversized.
    """
    limit = _channel_limit(channel)
    bucket = random.choices(
        ["empty", "short", "medium", "long", "very_long", "oversized"],
        weights=[10, 25, 30, 15, 10, 10],
    )[0]
    if bucket == "empty":
        return "", "empty"
    if bucket == "oversized":
        length = random.randint(limit + 1, min(limit + 1000, _SCHEMA_MESSAGE_CEILING))
        label = "oversized"
    elif bucket == "short":
        length = random.randint(1, 20)
        label = "short"
    elif bucket == "medium":
        length = random.randint(21, 200)
        label = "medium"
    elif bucket == "long":
        length = random.randint(201, 1000)
        label = "long"
    else:  # very_long
        length = random.randint(1001, limit)
        label = "very_long"
    if length <= 2:
        return random.choice("abz"), label
    n_words = max(1, length // 6)
    words = [random.choice(_WORDS) for _ in range(n_words)]
    text = " ".join(words)
    return text[:length], label


def _build_request(i: int, channel: str) -> tuple:
    """Build a randomized request for the given channel.

    Returns (payload_dict, message_type_label). The message may be empty
    or oversized, in which case the expected status is 422/413 (rejection).
    """
    contact = _random_contact(channel)
    message, msg_type = _random_message(channel)
    return {
        "channels": [{"channel": channel, "contact": contact}],
        "message": message,
        "reference": f"load-{channel}-{i}-{random.randint(1000, 9999)}",
    }, msg_type


def _expected_status_for(msg_len: int, msg_type: str, channel: str) -> int:
    """Return the expected HTTP status for a message:
    202 (valid) or 422 (rejected by schema) or 413 (rejected by channel limit).
    """
    if msg_len == 0 or msg_type == "empty":
        return 422
    limit = _channel_limit(channel)
    if msg_len > _SCHEMA_MESSAGE_CEILING:
        return 422
    if msg_len > limit:
        return 413
    return 202


def _short_reason(text: str, max_len: int = 80) -> str:
    """Truncate a reason/error message to a single short line (safe for terminal)."""
    if not text:
        return ""
    text = text.split("\n")[0].strip()
    if len(text) > max_len:
        text = text[:max_len - 3] + "..."
    return text


# ----------------------------------------------------------------- edge cases
# Intentionally invalid/boundary requests. Each has an expected error status;
# a correct rejection is NOT a server failure. Builders take the channel so
# oversized messages exceed that channel's configured limit (HTTP 413).
EDGE_CASES: List[Dict[str, Any]] = [
    {"name": "missing_channels", "expected_status": 422,
     "build": lambda ch: {"message": "no channels"}},
    {"name": "empty_channels", "expected_status": 422,
     "build": lambda ch: {"channels": [], "message": "x"}},
    {"name": "empty_message", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": ch, "contact": _random_contact(ch)}], "message": ""}},
    {"name": "message_too_long", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": ch, "contact": _random_contact(ch)}],
                          "message": "x" * (_SCHEMA_MESSAGE_CEILING + 1)}},
    {"name": "invalid_channel", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": "telegram", "contact": _random_contact(ch)}], "message": "x"}},
    {"name": "invalid_contact_too_short", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": ch, "contact": "a"}], "message": "x"}},
    {"name": "contact_wrong_type", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": ch, "contact": 12345}], "message": "x"}},
    {"name": "reference_too_long", "expected_status": 422,
     "build": lambda ch: {"channels": [{"channel": ch, "contact": _random_contact(ch)}],
                          "message": "x", "reference": "r" * 200}},
]


def _pick_edge_case() -> Dict[str, Any]:
    return dict(random.choice(EDGE_CASES))


# ----------------------------------------------------------------- validation
def _validate(status: int, body: Optional[Dict], contract: Dict[str, Any]) -> str:
    """Validate a valid-request response against a per-channel contract.

    Returns '' on PASS. The channel status is NOT pinned to "queued": in mock/
    dummy mode the API may legitimately return delivered or any valid Status
    (especially for duplicate replays).
    """
    expected_status = contract.get("status", 202)
    expected_fields = contract.get("fields", _EXPECTED_FIELDS)
    expected_channel = contract.get("channel")
    if status != expected_status:
        return f"expected status {expected_status}, got {status}"
    if not isinstance(body, dict):
        return f"body is not a dict: {_safe_body(body)}"
    missing = [f for f in expected_fields if f not in body]
    if missing:
        return f"missing fields: {missing}"
    channels = body.get("channels")
    if not isinstance(channels, list) or not channels:
        return "channels must be a non-empty list"
    first = channels[0]
    if not isinstance(first, dict):
        return "channels[0] is not an object"
    if first.get("channel") != expected_channel:
        return f"channels[0].channel={first.get('channel')} expected {expected_channel}"
    st = first.get("status")
    if not isinstance(st, str) or st not in VALID_STATUSES:
        return f"channels[0].status={st!r} is not a valid status"
    if not isinstance(first.get("message_id"), str):
        return "channels[0].message_id is not a string"
    if not isinstance(body.get("message_id"), str):
        return "message_id is not a string"
    return ""


def _classify(body: Optional[Dict]) -> str:
    """Classify a structurally-valid response outcome."""
    if not isinstance(body, dict):
        return "invalid_schema"
    if body.get("duplicate") is True:
        return "duplicate"
    channels = body.get("channels")
    if isinstance(channels, list) and channels and isinstance(channels[0], dict):
        st = channels[0].get("status")
        if st == "delivered":
            return "delivered"
        if st in ("queued", "processing", "submitted", "sent", "accepted"):
            return "queued"
        return "accepted"
    return "invalid_schema"


def _validate_edge(status: int, expected_status: int) -> str:
    """Validate an edge-case (intentionally invalid) request. Returns '' when
    the API correctly rejected it with the expected status."""
    if status == expected_status:
        return ""
    if status == 202:
        return f"expected rejection ({expected_status}), API ACCEPTED it instead"
    return f"expected status {expected_status}, got {status}"


def _classify_message_type(msg_len: int, channel: str = "sms") -> str:
    """Map a message length to a size bucket: short/medium/long/very_long/oversized."""
    limit = _channel_limit(channel)
    if msg_len <= 0:
        return "empty"
    if msg_len > _SCHEMA_MESSAGE_CEILING:
        return "oversized"
    if msg_len > limit:
        return "oversized"
    if msg_len <= 20:
        return "short"
    if msg_len <= 200:
        return "medium"
    if msg_len <= 1000:
        return "long"
    return "very_long"


# ---------------------------------------------------------------------- worker
def one_send(base: str, token: Union[str, "TokenRefresher"], i: int, channel: str = "sms",
             worker: str = "?", edge_case: Optional[Dict[str, Any]] = None,
             auth: Optional["TokenRefresher"] = None) -> Dict[str, Any]:
    """Send one request (valid or edge-case) and capture full detail.

    Returns keys: status, ok, body, parsed, error, error_type, latency_ms,
    request, request_num, channel, worker, validation_error, outcome, pass,
    message_length, message_type, expected_outcome, actual_outcome,
    token_refreshed, retry_count, timeout_ms, upstream, request_id.

    When `token` is a TokenRefresher it is used for auth refresh on 401;
    otherwise `auth` (if provided) is used.
    If the API returns HTTP 401 (token expired), a fresh JWT is obtained and
    the request is retried ONCE. That expected 401 is not counted as a failure.
    """
    refresher: Optional[TokenRefresher] = token if isinstance(token, TokenRefresher) else auth

    if edge_case:
        payload = edge_case["build"](channel)
        expected_status = edge_case["expected_status"]
        expected_outcome = "rejected"
        contract = {"status": expected_status, "fields": [], "channel": channel}
    else:
        payload, msg_type = _build_request(i, channel)
        expected_outcome = "accepted"
        contract = CHANNEL_CONTRACTS[channel]

    msg = payload.get("message")
    msg_len = len(msg) if isinstance(msg, str) else 0
    if edge_case:
        msg_type = edge_case["name"]
    else:
        expected_status = _expected_status_for(msg_len, msg_type, channel)
        if expected_status in (422, 413):
            expected_outcome = "rejected"
    configured_limit = _channel_limit(channel)

    start = time.perf_counter()
    attempts = 0
    max_attempts = 2
    token_refreshed = False
    result: Dict[str, Any] = {}
    timeout_ms: Optional[float] = None
    upstream: Optional[str] = None
    request_id: Optional[str] = None

    while attempts < max_attempts:
        attempts += 1
        try:
            current_token = refresher.token() if refresher else (token if isinstance(token, str) else "")
            status, body, meta = _post_json(base, "/api/v1/notifications/send", payload,
                                             token=current_token)
            request_id = meta.get("request_id") or request_id
            result = {
                "status": status, "ok": status == expected_status,
                "body": _safe_body(body), "parsed": body if isinstance(body, dict) else None,
                "error": None, "error_type": None,
            }
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and refresher is not None and attempts == 1:
                try:
                    refresher.refresh()
                    token_refreshed = True
                except Exception as rexc:  # noqa: BLE001
                    result = {
                        "status": None, "ok": False, "body": "", "parsed": None,
                        "error": f"auth refresh failed: {rexc}",
                        "error_type": "AuthRefreshError",
                        "auth_failed": True,
                        "auth_timeout": _is_timeout_error(rexc),
                    }
                    if _is_timeout_error(rexc):
                        result["timeout_ms"] = (time.perf_counter() - start) * 1000
                        result["upstream"] = "auth/login"
                    break
                continue
            if exc.code == 401 and refresher is not None and attempts == 2:
                result = {
                    "status": 401, "ok": False, "body": "", "parsed": None,
                    "error": "authentication failed after token refresh",
                    "error_type": "AuthFailed",
                    "auth_failed": True,
                    "auth_timeout": False,
                }
                break
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body_text = ""
            result = {
                "status": exc.code, "ok": False, "body": _safe_body(body_text), "parsed": None,
                "error": str(exc), "error_type": type(exc).__name__,
            }
            break
        except Exception as exc:  # noqa: BLE001 - timeout/connection errors
            is_timeout = _is_timeout_error(exc)
            timeout_ms = (time.perf_counter() - start) * 1000 if is_timeout else None
            result = {
                "status": None, "ok": False, "body": "", "parsed": None,
                "error": f"{type(exc).__name__}: {exc}", "error_type": type(exc).__name__,
                "timeout": is_timeout,
                "timeout_ms": timeout_ms,
                "upstream": "send" if is_timeout else None,
            }
            break

    elapsed = (time.perf_counter() - start) * 1000
    result["latency_ms"] = elapsed
    result["token_refreshed"] = token_refreshed
    result["retry_count"] = max(0, attempts - 1)
    result["request"] = _mask(payload)
    result["request_num"] = i
    result["channel"] = channel
    result["worker"] = worker
    result["message_length"] = msg_len
    result["message_type"] = msg_type
    result["configured_limit"] = configured_limit
    result["expected_status"] = expected_status
    result["rejection_reason"] = ""
    result["request_id"] = request_id
    result["timeout"] = result.get("timeout", False)
    result["timeout_ms"] = result.get("timeout_ms") or timeout_ms
    result["upstream"] = result.get("upstream") or upstream

    if expected_status == 202:
        verr = _validate(result["status"], result["parsed"], contract)
        result["outcome"] = _classify(result["parsed"])
        if result["status"] != expected_status:
            result["outcome"] = "http_failure"
        if verr:
            result["outcome"] = "invalid_schema"
        result["validation_error"] = verr
    else:
        # Expected rejection (empty/oversized/invalid): 422/413 unless the API
        # returns a different configured size-limit status.
        verr = _validate_edge(result["status"], expected_status)
        result["outcome"] = "expected_rejection" if not verr else "unexpected_accept"
        if result["status"] == 500:
            result["outcome"] = "http_failure"
        result["validation_error"] = verr
        if msg_len > configured_limit:
            result["rejection_reason"] = (
                f"Message too large: length {msg_len} exceeds the limit of "
                f"{configured_limit} characters"
            )
        elif msg_len == 0:
            result["rejection_reason"] = "Message is empty"
        else:
            result["rejection_reason"] = f"expected rejection (status {expected_status})"

    # PASS = valid outcomes + expected edge rejections. HTTP/schema failures
    # and unexpected acceptances FAIL.
    result["pass"] = not result["validation_error"]
    result["expected_outcome"] = expected_outcome
    result["actual_outcome"] = result["outcome"]
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
        self._by_channel: Dict[str, Dict[str, int]] = {}
        self._by_outcome: Dict[str, int] = {}
        self._by_message_type: Dict[str, int] = {}
        self._latencies = deque(maxlen=latency_window)
        self._completions: deque = deque()
        self._rps_window = rps_window_seconds
        self._started = time.monotonic()
        self._start_time = time.time()
        self._end_time: Optional[float] = None
        self._termination_reason: str = ""
        self._auth_failures = 0
        self._auth_timeouts = 0
        self._token_refreshes = 0
        self._retries = 0
        self._timeouts = 0
        self._max_latency = 0.0
        self._error_breakdown: Dict[str, int] = {}

    def record(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self._total += 1
            if result.get("pass"):
                self._ok += 1
            else:
                self._errors += 1
            key = str(result.get("status")) if result.get("status") is not None else "transport_error"
            self._by_status[key] = self._by_status.get(key, 0) + 1
            ch = result.get("channel", "?")
            entry = self._by_channel.setdefault(ch, {"ok": 0, "fail": 0})
            entry["ok" if result.get("pass") else "fail"] += 1
            self._by_outcome[result.get("outcome", "?")] = self._by_outcome.get(result.get("outcome", "?"), 0) + 1
            mt = result.get("message_type", "?")
            self._by_message_type[mt] = self._by_message_type.get(mt, 0) + 1
            lat = result.get("latency_ms")
            if lat is not None:
                self._latencies.append(lat)
                if lat > self._max_latency:
                    self._max_latency = lat
            self._completions.append(time.monotonic())
            now = time.monotonic()
            cutoff = now - self._rps_window
            while self._completions and self._completions[0] < cutoff:
                self._completions.popleft()

            if result.get("auth_failed"):
                self._auth_failures += 1
            if result.get("auth_timeout"):
                self._auth_timeouts += 1
            if result.get("token_refreshed"):
                self._token_refreshes += 1
            self._retries += result.get("retry_count", 0)
            if result.get("timeout"):
                self._timeouts += 1
            error_type = result.get("error_type") or result.get("outcome")
            if error_type and error_type != "?":
                self._error_breakdown[error_type] = self._error_breakdown.get(error_type, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
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
            by_channel = {k: dict(v) for k, v in self._by_channel.items()}
            by_outcome = dict(self._by_outcome)
            by_message_type = dict(self._by_message_type)
            auth_failures = self._auth_failures
            auth_timeouts = self._auth_timeouts
            token_refreshes = self._token_refreshes
            retries = self._retries
            timeouts = self._timeouts
            max_latency = self._max_latency
            error_breakdown = dict(self._error_breakdown)
            end_time = self._end_time
            termination_reason = self._termination_reason
            start_time = self._start_time
        s = sorted(lats)
        p50 = statistics.median(s) if s else None
        p95 = s[int(len(s) * 0.95) - 1] if s else None
        p99 = s[int(len(s) * 0.99) - 1] if s else None
        return {
            "total": total, "ok": ok, "errors": errors,
            "by_status": by_status, "by_channel": by_channel, "by_outcome": by_outcome,
            "by_message_type": by_message_type,
            "current_rps": current_rps, "avg_rps": avg_rps, "elapsed": elapsed,
            "p50": p50, "p95": p95, "p99": p99, "max_latency": max_latency,
            "auth_failures": auth_failures, "auth_timeouts": auth_timeouts,
            "token_refreshes": token_refreshes, "retries": retries,
            "timeouts": timeouts, "error_breakdown": error_breakdown,
            "start_time": start_time, "end_time": end_time,
            "termination_reason": termination_reason,
        }


def print_live_stats(stats: Stats) -> None:
    s = stats.snapshot()
    p50 = f"{s['p50']:.0f}" if s["p50"] is not None else "-"
    p95 = f"{s['p95']:.0f}" if s["p95"] is not None else "-"
    p99 = f"{s['p99']:.0f}" if s["p99"] is not None else "-"
    channels = " ".join(
        f"{ch}={v.get('ok', 0)}ok/{v.get('fail', 0)}fail"
        for ch, v in sorted(s["by_channel"].items())
    )
    print(
        f"[live] total={s['total']} ok={s['ok']} errors={s['errors']} "
        f"rps={s['avg_rps']:.1f} p50={p50}ms p95={p95}ms p99={p99}ms "
        f"channels=[{channels or '-'}]"
    )


def _run_live_printer(stats: Stats, stop_event: threading.Event,
                      interval: float = _LIVE_INTERVAL_SECONDS) -> None:
    while not stop_event.is_set():
        if stop_event.wait(interval):
            break
        print_live_stats(stats)


def _graceful_result(exc: BaseException) -> Dict[str, Any]:
    return {"status": None, "ok": False, "body": "",
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__, "latency_ms": 0.0, "outcome": "http_failure",
            "channel": "?", "worker": "?", "pass": False}


# Display modes for per-request terminal output (runtime toggleable).
DISPLAY_VERBOSE = "verbose"   # PASS + WARNING + ERROR
DISPLAY_WARN = "warnings"     # WARNING + ERROR (default)
DISPLAY_ERRORS = "errors"     # ERROR only
DISPLAY_QUIET = "quiet"       # no per-request lines (stats + summary only)


class Recorder:
    """Thread-safe per-request auditor with a runtime-toggleable display mode.

    Default (errors): only unexpected failures print ONE concise red line.
    Expected validation rejections and successful requests print nothing.
    verbose shows PASS + WARN + ERROR; warnings shows WARN + ERROR; quiet shows
    nothing per request. All full request/response detail is written to the
    JSONL log file regardless of mode.
    """

    def __init__(self, log_path: Optional[str], verbose: bool = False,
                 quiet: bool = False):
        self._lock = threading.Lock()
        if quiet:
            self._display = DISPLAY_QUIET
        elif verbose:
            self._display = DISPLAY_VERBOSE
        else:
            self._display = DISPLAY_WARN  # WARN (expected rejection) + ERROR
        self._fh = None
        if log_path:
            self._fh = open(log_path, "a", encoding="utf-8")

    def set_display(self, mode: str) -> None:
        with self._lock:
            self._display = mode

    def get_display(self) -> str:
        with self._lock:
            return self._display

    def record(self, r: Dict[str, Any]) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        num = r.get("request_num", "?")
        status = r.get("status")
        passed = r.get("pass", False)
        verr = r.get("validation_error") or ""
        channel = r.get("channel", "?")
        outcome = r.get("outcome", "accepted")
        endpoint = "/api/v1/notifications/send"
        contract = CHANNEL_CONTRACTS.get(channel, {})
        exp_status = r.get("expected_status", contract.get("status", 202))
        exp_fields = contract.get("fields", _EXPECTED_FIELDS)
        msg_len = r.get("message_length", "?")
        display = self.get_display()

        if display == DISPLAY_QUIET:
            pass  # no per-request terminal output
        elif r.get("auth_failed") and r.get("error_type") == "AuthFailed":
            # Refreshed token also received 401 - print AUTH_FAILED ERROR.
            reason = _short_reason(r.get("error") or "authentication failed after token refresh")
            print(_red(f"ERROR #{num} AUTH_FAILED status={status} "
                       f"reason={reason}"))
        elif not passed:
            # Only unexpected failures print - ONE concise red line.
            reason = _short_reason(
                verr or r.get("error") or r.get("rejection_reason") or "unexpected response"
            )
            print(_red(f"ERROR #{num} ch={channel} status={status} expected={exp_status} "
                       f"reason={reason} len={msg_len}"))
        elif outcome == "expected_rejection" and display in (DISPLAY_VERBOSE, DISPLAY_WARN):
            # Expected rejections (empty/too short/oversized): concise yellow WARN.
            print(_yellow(f"WARN #{num} ch={channel} status={status} expected={exp_status} "
                          f"len={msg_len} type={r.get('message_type', '?')} "
                          f"reason={_short_reason(r.get('rejection_reason') or 'expected rejection')}"))
        elif display == DISPLAY_VERBOSE:
            print(_green(f"PASS #{num} ch={channel} status={status} outcome={outcome} len={msg_len}"))

        if self._fh:
            record = {
                "timestamp": ts, "request_num": num, "pass": passed,
                "outcome": outcome,
                "expected_outcome": r.get("expected_outcome"),
                "actual_outcome": r.get("actual_outcome", outcome),
                "endpoint": endpoint, "channel": channel,
                "contact": _safe_contact(r.get("request")),
                "message_length": r.get("message_length"),
                "message_type": r.get("message_type"),
                "configured_limit": r.get("configured_limit"),
                "expected_status": exp_status,
                "expected_fields": exp_fields, "actual_status": status,
                "rejection_reason": r.get("rejection_reason") or None,
                "actual_body": r.get("body", ""), "latency_ms": r.get("latency_ms"),
                "validation_error": verr or None, "error": r.get("error"),
                "request": r.get("request"),
                "token_refreshed": r.get("token_refreshed", False),
                "retry_count": r.get("retry_count", 0),
                "auth_failed": r.get("auth_failed", False),
                "auth_timeout": r.get("auth_timeout", False),
                "timeout": r.get("timeout", False),
                "timeout_ms": r.get("timeout_ms"),
                "upstream": r.get("upstream"),
                "request_id": r.get("request_id"),
            }
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()


class SummaryReporter:
    """Persistent endurance-test reporter.

    Continuously writes a stats snapshot to a summary .log file (JSON-lines),
    records unexpected errors with a stop reason immediately, and writes a
    final report on Ctrl+C/stop. Supports separate files per terminal by
    passing a distinct --summary-file path.
    """

    def __init__(self, path: Optional[str], started_at: Optional[float] = None):
        self.path = path
        self.started_at = started_at if started_at is not None else time.time()
        self._lock = threading.Lock()
        self._fh = None
        if path:
            self._fh = open(path, "a", encoding="utf-8")
            self._write({
                "event": "start",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "started_at": self.started_at,
                "base_url": os.environ.get("BASE_URL", ""),
            })

    def _write(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            if self._fh:
                self._fh.write(json.dumps(rec, default=str) + "\n")
                self._fh.flush()

    def _ts(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self, stats: Stats, last_request: Optional[Dict] = None) -> None:
        s = stats.snapshot()
        self._write({
            "event": "stats",
            "timestamp": self._ts(),
            "elapsed": round(s["elapsed"], 3),
            "requests": s["total"],
            "ok": s["ok"],
            "expected_rejections": s["by_outcome"].get("expected_rejection", 0),
            "errors": s["errors"],
            "rps": round(s["avg_rps"], 3),
            "p50": s["p50"],
            "p95": s["p95"],
            "p99": s["p99"],
            "by_channel": s["by_channel"],
            "auth_failures": s["auth_failures"],
            "auth_timeouts": s["auth_timeouts"],
            "token_refreshes": s["token_refreshes"],
            "last_request": last_request,
        })

    def error(self, reason: str, detail: Optional[str] = None) -> None:
        self._write({
            "event": "error",
            "timestamp": self._ts(),
            "reason": reason,
            "detail": _short_reason(detail or "") or None,
        })

    def stop(self, reason: str, stats: Stats, last_request: Optional[Dict] = None) -> None:
        if not self._fh:
            return
        s = stats.snapshot()
        self._write({
            "event": "final",
            "timestamp": self._ts(),
            "stop_reason": reason,
            "elapsed": round(s["elapsed"], 3),
            "requests": s["total"],
            "ok": s["ok"],
            "expected_rejections": s["by_outcome"].get("expected_rejection", 0),
            "errors": s["errors"],
            "rps": round(s["avg_rps"], 3),
            "p50": s["p50"],
            "p95": s["p95"],
            "p99": s["p99"],
            "by_channel": s["by_channel"],
            "auth_failures": s["auth_failures"],
            "auth_timeouts": s["auth_timeouts"],
            "token_refreshes": s["token_refreshes"],
            "last_request": last_request,
        })
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def _keyboard_listener(recorder: Recorder, stats: Stats,
                       stop_event: threading.Event) -> None:
    """Daemon thread: read keys from stdin and toggle the display mode / print
    stats on demand. Only runs when stdin is a TTY (interactive terminal)."""
    if not sys.stdin.isatty():
        return
    print(_yellow(
        "[controls] v=verbose w=warnings e=errors q=quiet s=stats h=help"
    ))
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:  # noqa: BLE001 - stdin closed
            break
        if not line:
            break
        c = line.strip().lower()
        if c == "v":
            recorder.set_display(DISPLAY_VERBOSE)
            print(f"[display] verbose (all)")
        elif c == "w":
            recorder.set_display(DISPLAY_WARN)
            print(f"[display] warnings+errors")
        elif c == "e":
            recorder.set_display(DISPLAY_ERRORS)
            print(f"[display] errors only")
        elif c == "q":
            recorder.set_display(DISPLAY_QUIET)
            print(f"[display] quiet (stats only)")
        elif c == "s":
            print_live_stats(stats)
        elif c == "h":
            print(_yellow(
                "v=verbose  w=warnings+errors  e=errors-only  "
                "q=quiet  s=stats  h=help"
            ))


def run_continuous(base: str, token: Union[str, "TokenRefresher"], concurrency: int,
                   stop_event: Optional[threading.Event] = None,
                   sender: Callable[..., Dict[str, Any]] = one_send,
                   live_interval: float = _LIVE_INTERVAL_SECONDS,
                   log_file: Optional[str] = None, fail_fast: bool = False,
                   verbose: bool = False, quiet: bool = False,
                   weights: Optional[Dict[str, float]] = None,
                   edge_pct: float = 5.0,
                   summary_file: Optional[str] = None,
                   refresher: Optional["TokenRefresher"] = None) -> Stats:
    if stop_event is None:
        stop_event = threading.Event()

        def _on_sigint(signum, frame):  # noqa: ANN001
            print("\nCaught Ctrl+C - stopping...")
            stop_event.set()

        signal.signal(signal.SIGINT, _on_sigint)

    if not weights:
        weights = {c: 1.0 for c in CHANNEL_CONTRACTS}
    channels = list(weights.keys())
    channel_weights = [weights[c] for c in channels]
    edge_pct = max(0.0, min(100.0, edge_pct))

    stats = Stats()
    recorder = Recorder(log_file, verbose=verbose, quiet=quiet)
    summary = SummaryReporter(summary_file)
    counter = itertools.count(1)
    live_thread = threading.Thread(
        target=_run_live_printer, args=(stats, stop_event, live_interval), daemon=True
    )
    live_thread.start()
    kb_thread = threading.Thread(
        target=_keyboard_listener, args=(recorder, stats, stop_event), daemon=True
    )
    kb_thread.start()

    last_request: Optional[Dict[str, Any]] = None

    def _summary_loop() -> None:
        while not stop_event.is_set():
            if stop_event.wait(live_interval):
                break
            summary.snapshot(stats, last_request)

    if summary_file:
        threading.Thread(target=_summary_loop, daemon=True).start()

    def _pick():
        ch = random.choices(channels, weights=channel_weights, k=1)[0]
        edge = _pick_edge_case() if (random.random() * 100.0) < edge_pct else None
        return ch, edge

    def _submit(ex, i):
        ch, edge = _pick()
        worker = f"w{(i - 1) % concurrency}"
        return ex.submit(sender, base, token, i, ch, worker, edge)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = set()
        for _ in range(concurrency):
            futs.add(_submit(ex, next(counter)))

        while not stop_event.is_set() and futs:
            # Check if auth has permanently failed and stop cleanly.
            if refresher and refresher.failed:
                print(f"\nAuth permanently failed: {refresher.last_error}. Stopping.")
                stop_event.set()
                break

            done, _pending = concurrent.futures.wait(
                futs, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in done:
                futs.discard(fut)
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    result = _graceful_result(exc)
                stats.record(result)
                recorder.record(result)
                last_request = {
                    "request_num": result.get("request_num"),
                    "channel": result.get("channel"),
                    "status": result.get("status"),
                    "outcome": result.get("outcome"),
                    "pass": result.get("pass"),
                    "message_length": result.get("message_length"),
                    "message_type": result.get("message_type"),
                }
                if not result.get("pass", False):
                    summary.error(
                        "unexpected_failure",
                        f"{result.get('validation_error') or result.get('error') or 'unexpected response'}",
                    )
                if fail_fast and not result.get("pass", False):
                    print(f"FAIL-FAST: request #{result.get('request_num')} did not pass; stopping.")
                    stop_event.set()
                    break
                if not stop_event.is_set():
                    futs.add(_submit(ex, next(counter)))

        for fut in futs:
            fut.cancel()
        ex.shutdown(wait=True, cancel_futures=True)

    summary.stop("ctrl_c_or_fail_fast", stats, last_request)
    recorder.close()
    return stats


def summarize(results: List[Dict[str, Any]], total_requests: int, total_seconds: float) -> Dict[str, Any]:
    latencies = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
    ok = sum(1 for r in results if r.get("pass"))
    errors = len(results) - ok
    by_status: Dict[str, int] = {}
    representative: Dict[str, str] = {}
    by_outcome: Dict[str, int] = {}
    by_channel: Dict[str, Dict[str, int]] = {}
    by_message_type: Dict[str, int] = {}
    error_breakdown: Dict[str, int] = {}
    auth_failures = 0
    auth_timeouts = 0
    token_refreshes = 0
    retries = 0
    timeouts = 0
    max_latency = 0.0
    for r in results:
        by_outcome[r.get("outcome", "?")] = by_outcome.get(r.get("outcome", "?"), 0) + 1
        ch = r.get("channel", "?")
        entry = by_channel.setdefault(ch, {"ok": 0, "fail": 0})
        entry["ok" if r.get("pass") else "fail"] += 1
        mt = r.get("message_type", "?")
        by_message_type[mt] = by_message_type.get(mt, 0) + 1
        lat = r.get("latency_ms", 0) or 0
        if lat > max_latency:
            max_latency = lat
        if r.get("pass"):
            continue
        key = str(r.get("status")) if r.get("status") is not None else "transport_error"
        by_status[key] = by_status.get(key, 0) + 1
        body = r.get("body") or r.get("error") or ""
        if key not in representative and body:
            representative[key] = body
        if r.get("auth_failed"):
            auth_failures += 1
        if r.get("auth_timeout"):
            auth_timeouts += 1
        if r.get("token_refreshed"):
            token_refreshes += 1
        retries += r.get("retry_count", 0)
        if r.get("timeout"):
            timeouts += 1
        etype = r.get("error_type") or r.get("outcome")
        if etype and etype != "?":
            error_breakdown[etype] = error_breakdown.get(etype, 0) + 1
    s = sorted(latencies)
    return {
        "requests": total_requests, "ok": ok, "errors": errors,
        "total_seconds": total_seconds,
        "throughput": total_requests / total_seconds if total_seconds else 0.0,
        "by_status": dict(sorted(by_status.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)),
        "by_outcome": by_outcome, "representative": representative,
        "by_channel": by_channel, "by_message_type": by_message_type,
        "p50": statistics.median(s) if s else None,
        "p95": s[int(len(s) * 0.95) - 1] if s else None,
        "p99": s[int(len(s) * 0.99) - 1] if s else None,
        "max_latency": max_latency,
        "auth_failures": auth_failures, "auth_timeouts": auth_timeouts,
        "token_refreshes": token_refreshes, "retries": retries,
        "timeouts": timeouts, "error_breakdown": error_breakdown,
    }


def print_report(summary: Dict[str, Any]) -> None:
    print("\n--- results ---")
    print(f"requests:      {summary['requests']}")
    print(f"ok:            {summary['ok']}")
    print(f"errors:        {summary['errors']}")
    print(f"total time:    {summary['total_seconds']:.2f}s")
    print(f"throughput:    {summary['throughput']:.1f} req/s")
    if summary["p50"] is not None:
        print(f"latency p50:   {summary['p50']:.1f} ms")
    if summary["p95"] is not None:
        print(f"latency p95:   {summary['p95']:.1f} ms")
    if summary["p99"] is not None:
        print(f"latency p99:   {summary['p99']:.1f} ms")
    if summary.get("max_latency"):
        print(f"latency max:   {summary['max_latency']:.1f} ms")
    print(f"auth failures:     {summary.get('auth_failures', 0)}")
    print(f"auth timeouts:     {summary.get('auth_timeouts', 0)}")
    print(f"token refreshes:   {summary.get('token_refreshes', 0)}")
    print("outcomes:")
    for out, cnt in sorted(summary["by_outcome"].items()):
        print(f"  {out}: {cnt}")
    if summary.get("by_message_type"):
        print("message types:")
        for mt, cnt in sorted(summary["by_message_type"].items()):
            print(f"  {mt}: {cnt}")
    if summary.get("by_channel"):
        print("per channel:")
        for ch, v in sorted(summary["by_channel"].items()):
            print(f"  {ch}: {v.get('ok', 0)} ok / {v.get('fail', 0)} fail")
    if summary.get("error_breakdown"):
        print("error breakdown:")
        for err, cnt in sorted(summary["error_breakdown"].items()):
            print(f"  {err}: {cnt}")


def print_continuous_summary(stats: Stats) -> None:
    s = stats.snapshot()
    p50 = f"{s['p50']:.1f}" if s["p50"] is not None else "-"
    p95 = f"{s['p95']:.1f}" if s["p95"] is not None else "-"
    p99 = f"{s['p99']:.1f}" if s["p99"] is not None else "-"
    max_lat = f"{s['max_latency']:.1f}" if s.get("max_latency") else "-"
    print("\n--- final summary ---")
    print(f"requests:              {s['total']}")
    print(f"ok:                    {s['ok']}")
    print(f"errors:                {s['errors']}")
    print(f"total time:            {s['elapsed']:.2f}s")
    print(f"average RPS:           {s['avg_rps']:.1f}")
    print(f"latency p50:           {p50} ms")
    print(f"latency p95:           {p95} ms")
    print(f"latency p99:           {p99} ms")
    print(f"latency max:           {max_lat} ms")
    print(f"authentication failures: {s.get('auth_failures', 0)}")
    print(f"authentication timeouts: {s.get('auth_timeouts', 0)}")
    print(f"token refreshes:        {s.get('token_refreshes', 0)}")
    print("per channel:")
    for ch, v in sorted(s["by_channel"].items()):
        print(f"  {ch}: {v.get('ok', 0)} ok / {v.get('fail', 0)} fail")
    print("outcomes:")
    for out, cnt in sorted(s["by_outcome"].items()):
        print(f"  {out}: {cnt}")
    if s.get("by_message_type"):
        print("message types:")
        for mt, cnt in sorted(s["by_message_type"].items()):
            print(f"  {mt}: {cnt}")
    if s.get("error_breakdown"):
        print("error breakdown:")
        for err, cnt in sorted(s["error_breakdown"].items()):
            print(f"  {err}: {cnt}")
    if s.get("termination_reason"):
        print(f"termination reason: {s['termination_reason']}")


def _parse_weights(spec: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in spec.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip().lower()
            if k in CHANNEL_CONTRACTS:
                out[k] = max(0.0, float(v))
    if not out:
        out = {c: 1.0 for c in CHANNEL_CONTRACTS}
    return out


def main():
    parser = argparse.ArgumentParser(description="Realistic randomized load test (mock mode, no real sends)")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="Server base URL")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--requests", type=int, default=200,
                        help="Number of requests (finite mode; ignored with --continuous)")
    parser.add_argument("--continuous", action="store_true", help="Run until Ctrl+C")
    parser.add_argument("--weights", default="sms=1,whatsapp=1,email=1",
                        help="Channel weights e.g. sms=50,whatsapp=30,email=20")
    parser.add_argument("--edge-pct", type=float, default=5.0,
                        help="Percent of requests that are intentional edge cases")
    parser.add_argument("--log-file", default=None, help="Write per-request JSONL results")
    parser.add_argument("--summary-file", default=None,
                        help="Persistent summary .log (start/stats/error/final). Use a "
                             "separate file per concurrent terminal, e.g. run1_summary.log")
    parser.add_argument("--fail-fast", action="store_true", help="Abort on first unexpected response")
    parser.add_argument("--verbose", action="store_true", help="Also print PASS lines")
    parser.add_argument("--quiet", action="store_true", help="Suppress all per-request lines")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--client-id", default=None, help="AUTH_CLIENT_ID (default from .env)")
    parser.add_argument("--client-secret", default=None, help="AUTH_CLIENT_SECRET (default from .env)")
    args = parser.parse_args()

    global _USE_COLOR
    _USE_COLOR = not args.no_color and sys.stdout.isatty()

    weights = _parse_weights(args.weights)

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

    # Create the TokenRefresher for automatic JWT refresh during the test.
    summary = None
    if args.summary_file:
        summary = SummaryReporter(args.summary_file)
    auth = TokenRefresher(args.base_url, client_id, client_secret,
                          initial_token=token, summary=summary)

    started_at = time.time()

    if args.continuous:
        print(f"Obtained JWT. Continuous mode: {args.concurrency} workers, weights={weights}, "
              f"edge_pct={args.edge_pct}% until Ctrl+C...")
        stats = run_continuous(args.base_url, auth, args.concurrency,
                               log_file=args.log_file, fail_fast=args.fail_fast,
                               verbose=args.verbose, quiet=args.quiet,
                               weights=weights, edge_pct=args.edge_pct,
                               summary_file=args.summary_file,
                               refresher=auth)
        print_continuous_summary(stats)
        # If auth permanently failed, exit with a distinct code.
        if auth.failed:
            print(f"Test stopped: {auth.last_error}")
            sys.exit(2)
        sys.exit(0 if stats.snapshot()["errors"] == 0 else 1)

    print(f"Obtained JWT. Running {args.requests} requests with {args.concurrency} workers...")
    started = time.perf_counter()
    results: List[Dict[str, Any]] = []
    recorder = Recorder(args.log_file, verbose=args.verbose, quiet=args.quiet)
    channels = list(weights.keys())
    cw = [weights[c] for c in channels]

    # Before starting the main loop, fail fast if auth is already broken.
    try:
        auth.token()
    except RuntimeError as exc:
        recorder.close()
        print(f"Auth failed at startup: {exc}")
        sys.exit(2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = []
        for i in range(args.requests):
            ch = random.choices(channels, weights=cw, k=1)[0]
            edge = _pick_edge_case() if (random.random() * 100.0) < args.edge_pct else None
            futs.append(ex.submit(one_send, args.base_url, auth, i, ch, f"w{i % args.concurrency}", edge))
        for fut in concurrent.futures.as_completed(futs):
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = _graceful_result(exc)
            results.append(result)
            recorder.record(result)
            if args.fail_fast and not result.get("pass", False):
                print(f"FAIL-FAST: request #{result.get('request_num')} did not pass; stopping.")
                break
    recorder.close()

    total_seconds = time.perf_counter() - started
    summary = summarize(results, len(results), total_seconds)
    print_report(summary)
    if auth.failed:
        print(f"Auth failed during test: {auth.last_error}")
        sys.exit(2)
    sys.exit(0 if summary["errors"] == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted - exiting.")
        sys.exit(130)