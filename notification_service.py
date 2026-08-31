#!/usr/bin/env python3
"""
Notification Service CLI - run it like any Linux command.

  python3 notification_service.py                  interactive menu
  python3 notification_service.py send whatsapp 9887270348 "Hello"
  python3 notification_service.py send sms     9887270348 "Your OTP is 482913"
  python3 notification_service.py send email   you@example.com "Order shipped"
  python3 notification_service.py send email,whatsapp +919887270348 "Order shipped"
  python3 notification_service.py send email you@example.com "Order shipped" --template default
  python3 notification_service.py send sms 9887270348 "Resend this" --resend
  python3 notification_service.py send-template <number> <template_name> [--param name=value ...] [--resend]
  python3 notification_service.py send-event event.json
  python3 notification_service.py status <message_id>
  python3 notification_service.py -v status <message_id>
  python3 notification_service.py audit [--limit N] [--user <id>]
  python3 notification_service.py logs [-f]
  python3 notification_service.py db-check

The server is started automatically if it isn't running.
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
PORT = 8000
LOG_FILE = "/tmp/notification-service.log"


def _log_paths() -> tuple:
    """Read the configured app-log and audit-log paths from .env (or defaults)."""
    try:
        from app.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        app_log = getattr(s, "LOG_FILE", "") or LOG_FILE
        audit_log = getattr(s, "AUDIT_LOG_FILE", "") or ""
        return app_log, audit_log
    except Exception:
        return LOG_FILE, ""
VERBOSE = False


def _auth_api_key() -> str:
    """Load AUTH_API_KEY from the application settings (.env).

    Single source of truth: AUTH_ENABLED gates authentication and AUTH_API_KEY
    is the only key used by both the server (app/auth.py) and this CLI.
    """
    try:
        from app.config import get_settings

        get_settings.cache_clear()
        return get_settings().AUTH_API_KEY or ""
    except Exception:
        return ""


API_KEY = _auth_api_key()

_JWT_TOKEN = ""
_JWT_CLIENT_ID = ""
_JWT_CLIENT_SECRET = ""


def _auth_client_id() -> str:
    try:
        from app.config import get_settings
        get_settings.cache_clear()
        return get_settings().AUTH_CLIENT_ID or ""
    except Exception:
        return ""


def _auth_client_secret() -> str:
    try:
        from app.config import get_settings
        get_settings.cache_clear()
        return get_settings().auth_client_secret_effective or ""
    except Exception:
        return ""


def _jwt_login() -> str:
    """Obtain a JWT from the server and cache it."""
    global _JWT_TOKEN, _JWT_CLIENT_ID, _JWT_CLIENT_SECRET
    if not _JWT_CLIENT_ID:
        _JWT_CLIENT_ID = _auth_client_id()
        _JWT_CLIENT_SECRET = _auth_client_secret()
    if not _JWT_CLIENT_ID or not _JWT_CLIENT_SECRET:
        return ""
    data = json.dumps({"client_id": _JWT_CLIENT_ID, "client_secret": _JWT_CLIENT_SECRET}).encode()
    req = urllib.request.Request(
        BASE_URL + "/api/v1/auth/login", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            _JWT_TOKEN = body.get("access_token", "")
            return _JWT_TOKEN
    except Exception:
        return ""


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------- helpers

def _json_detail(exc):
    """Best-effort parse of an HTTPError body."""
    try:
        return json.loads(exc.read().decode())
    except Exception:
        return {"detail": str(exc)}


def _auth_headers(headers: dict) -> dict:
    """Add the auth header: Bearer JWT (preferred) or legacy X-API-Key."""
    global _JWT_TOKEN
    if not _JWT_TOKEN and _SERVER_REQUIRES_AUTH:
        _JWT_TOKEN = _jwt_login()
    if _JWT_TOKEN:
        headers = dict(headers)
        headers["Authorization"] = f"Bearer {_JWT_TOKEN}"
        return headers
    key = API_KEY
    if _SERVER_REQUIRES_AUTH and not key:
        key = _ensure_api_key()
    if key:
        headers = dict(headers)
        headers["X-API-Key"] = key
    return headers


def http(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers = _auth_headers(headers)
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and _SERVER_REQUIRES_AUTH:
            # Re-login (JWT may have expired) and retry once.
            global _JWT_TOKEN
            _JWT_TOKEN = ""
            headers2 = _auth_headers(headers)
            req2 = urllib.request.Request(
                BASE_URL + path, data=data, headers=headers2, method=method,
            )
            try:
                with urllib.request.urlopen(req2, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc2:
                return exc2.code, _json_detail(exc2)
        return exc.code, _json_detail(exc)


def http_full(method: str, path: str, payload: dict | None = None):
    """Like http() but returns (status, headers, body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers = _auth_headers(headers)
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and _SERVER_REQUIRES_AUTH:
            # Re-login (JWT may have expired) and retry once.
            global _JWT_TOKEN
            _JWT_TOKEN = ""
            headers2 = _auth_headers(headers)
            req2 = urllib.request.Request(BASE_URL + path, data=data, headers=headers2, method=method)
            try:
                with urllib.request.urlopen(req2, timeout=5) as resp:
                    return resp.status, dict(resp.headers), json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc2:
                return exc2.code, {}, _json_detail(exc2)
        return exc.code, dict(exc.headers), _json_detail(exc)


def server_up() -> bool:
    try:
        code, body = http("GET", "/health")
        if code != 200:
            return False
        global _SERVER_REQUIRES_AUTH
        _SERVER_REQUIRES_AUTH = bool(body.get("auth_enabled", False))
        return True
    except Exception:
        return False


_SERVER_REQUIRES_AUTH = False


def _ensure_api_key() -> str:
    """Return AUTH_API_KEY (from .env) without prompting.

    The CLI and server share the same settings, so when AUTH_API_KEY is
    configured the key is already loaded and there is no need to prompt.
    Only when auth is enabled and no key is configured (misconfiguration) do
    we prompt as a last resort so the CLI still works.
    """
    global API_KEY
    if API_KEY:
        return API_KEY
    if not _SERVER_REQUIRES_AUTH:
        return ""
    try:
        API_KEY = input("API key: ").strip()
    except EOFError:
        print("No API key configured. Set AUTH_API_KEY in .env.")
        sys.exit(1)
    return API_KEY


def kill_port_holder() -> None:
    """Kill whatever process holds PORT plus any orphaned project servers.

    Orphaned uvicorn reload workers keep respawning and re-binding the port,
    so we kill every process whose command line mentions this project's
    uvicorn binary before returning.
    """
    hex_port = f"{PORT:04X}"
    killed = []
    project_bin = str(ROOT / "venv" / "bin" / "uvicorn")
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/net/tcp") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == "0A":
                        if parts[1].split(":")[1] == hex_port:
                            os.kill(int(pid), 9)
                            killed.append(pid)
        except (OSError, ValueError, PermissionError):
            continue
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\x00", b" ").decode(errors="replace")
            except (OSError, PermissionError):
                continue
            if "spawn_main" in cmd or (project_bin in cmd and "notification" in cmd):
                os.kill(int(pid), 9)
                killed.append(pid)
    except (OSError, ValueError):
        pass
    if killed:
        print(f"Killed stuck process(es) on port {PORT}: {', '.join(killed)}")
        time.sleep(2)


def ensure_server() -> bool:
    if server_up():
        return True

    sock = __import__("socket").socket()
    try:
        sock.bind(("127.0.0.1", PORT))
        port_free = True
    except OSError:
        port_free = False
    finally:
        sock.close()

    if not port_free:
        kill_port_holder()

    print(f"Starting server at {BASE_URL}...")
    # Let the server process inherit stdout/stderr so logs appear in the
    # terminal in real time. The app writes its own rotating log file via the
    # configured LOG_FILE (we no longer redirect to LOG_FILE here, which
    # previously caused duplicate log lines).
    cmd = [str(ROOT / "run.sh")]
    if not (ROOT / "run.sh").exists():
        cmd = [str(ROOT / "venv" / "bin" / "uvicorn"), "app.main:app",
               "--host", "127.0.0.1", "--port", str(PORT)]
    subprocess.Popen(cmd, cwd=ROOT, stdout=None, stderr=None,
                     start_new_session=True)

    for _ in range(30):
        time.sleep(1)
        if server_up():
            print(f"Server ready at {BASE_URL}")
            print(f"Server logs   : {LOG_FILE}  (view with: python3 notification_service.py logs)")
            return True
    print(f"Server failed to start. Check {LOG_FILE}", file=sys.stderr)
    return False


def load_env() -> dict:
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def config_check() -> None:
    env = load_env()
    mock = env.get("MOCK_MODE", "true").lower() == "true"
    if mock:
        print("! MOCK MODE is ON - messages are simulated, NOTHING is actually sent.")
        print("  To receive real messages: set MOCK_MODE=false in .env and add your")
        print("  Azure Communication Services connection string (covers all 3 channels).")
        print()
        return

    conn = env.get("COMMUNICATION_SERVICES_CONNECTION_STRING") or env.get("AZURE_COMMUNICATION_CONNECTION_STRING", "")
    twilio_ready = bool(env.get("TWILIO_ACCOUNT_SID")) and bool(env.get("TWILIO_AUTH_TOKEN"))
    if not conn or "your_" in conn:
        if twilio_ready:
            print("! COMMUNICATION_SERVICES_CONNECTION_STRING is missing/placeholder - Email sends will fail.")
            print("  (SMS/WhatsApp use Twilio; Azure is only required for the Email channel.)")
        else:
            print("! MOCK_MODE=false but COMMUNICATION_SERVICES_CONNECTION_STRING is")
            print("  missing/placeholder in .env. Real sends will fail until you add it")
            print("  (Azure portal -> Communication Services -> Keys -> Connection string).")
        print()
        return
    if not env.get("AZURE_SMS_FROM") and not (twilio_ready and env.get("TWILIO_FROM")):
        print("  (sms) AZURE_SMS_FROM is empty (and no Twilio TWILIO_FROM) - SMS sends will fail")
    if not env.get("AZURE_EMAIL_FROM"):
        print("  (email) AZURE_EMAIL_FROM is empty - Email sends will fail")
    if not (env.get("WHATSAPP_CHANNEL_ID") or env.get("AZURE_WHATSAPP_CHANNEL_ID")) and not (twilio_ready and (env.get("TWILIO_WHATSAPP_FROM") or env.get("TWILIO_FROM"))):
        print("  (whatsapp) WHATSAPP_CHANNEL_ID is empty (and no Twilio sender) - WhatsApp sends will fail")
    print()
    print("  Note: WhatsApp outbound to a new contact needs an approved Meta")
    print("  template; free text only works inside a 24h session window.")
    print()


# ---------------------------------------------------------------- actions

def _print_recent_logs(lines: int = 8, delay: float = 3.0,
                        notification_id: str = "", group_id: str = "") -> None:
    """Show the log lines for the notification that was just acted on.

    The CLI auto-starts the API as a detached background process, so its
    stdout is not attached to this terminal. Instead of printing the whole
    tail (which mixes unrelated notifications), filter the durable app log
    (logs/app.log) by the notification_id / group_id of this action so only
    the relevant lifecycle lines appear.
    """
    app_log, _ = _log_paths()
    time.sleep(delay)  # let the async worker flush its log lines
    needles = [x for x in (notification_id, group_id) if x]
    try:
        with open(app_log, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except FileNotFoundError:
        print(f"  (no application log yet at {app_log})")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not read app log: {exc})")
        return

    if needles:
        matched = [ln for ln in all_lines if any(n in ln for n in needles)]
    else:
        matched = all_lines
    matched = matched[-lines:]
    if not matched:
        print("  (no log lines matched this notification yet)")
        return
    print("  --- notification log ---")
    for ln in matched:
        ln = ln.rstrip("\n")
        if ln.strip():
            print(f"  {ln}")
    print("  -------------------------")


def _normalize_channel(ch: str) -> str:
    """Normalize channel input: trim + case-insensitive ("SMS" -> "sms")."""
    return ch.strip().lower()


def do_send_entries(entries: list, message: str, force: bool = False) -> None:
    """POST pre-built channel entries (each with its own channel/contact/template).

    `force=True` passes `resend=true` so the server creates a new notification
    on duplicate instead of returning the existing one.
    """
    payload = {"channels": entries, "message": message}
    if force:
        payload["resend"] = True
    code, resp_headers, body = http_full("POST", "/api/v1/notifications/send", payload)

    # Duplicate detected: show the clear message and ask whether to resend.
    if not force and (
        resp_headers.get("X-Idempotent-Replay", "").lower() == "true"
        or body.get("duplicate")
    ):
        dup_msg = body.get("message") or "This message was already sent. Do you want to resend?"
        print(dup_msg)
        print(f"Existing message id: {body.get('message_id', '?')} (status: {body.get('status', '?')})")
        try:
            answer = input("Do you want to resend? (y/n): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer == "y":
            payload["resend"] = True
            code, resp_headers, body = http_full("POST", "/api/v1/notifications/send", payload)
        else:
            if body.get("message_id"):
                print(f"Returning existing notification: {body['message_id']} (status: {body.get('status')})")
                return
    if code != 202 or not body.get("message_id"):
        detail = body.get("error", body.get("detail", body))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        print(f"Error: {detail}")
        sys.exit(1)
    group_id = body["message_id"]
    templates = {e["channel"]: e.get("template_name", "") for e in entries}
    print(f"Queued {len(entries)} channel(s):")
    for c in body.get("channels", []):
        tpl = f", template={templates.get(c['channel'])}" if templates.get(c["channel"]) else ""
        print(f"  - {c['channel']} -> {c['contact']}  (message id: {c['message_id']}{tpl})")
    print(f"Group id    : {group_id}")
    print(f"Check status: python3 notification_service.py status {group_id}")
    if VERBOSE:
        print(json.dumps(body, indent=2))
    first_id = body["channels"][0]["message_id"] if body.get("channels") else group_id
    _print_recent_logs(notification_id=first_id, group_id=group_id)


def do_send(channels: list, message: str, template: str = "", template_params: dict | None = None,
            force: bool = False) -> None:
    entries = []
    for ch, ct in channels:
        entry = {"channel": _normalize_channel(ch), "contact": ct}
        if template:
            entry["template_name"] = template
        if template_params:
            entry["template_params"] = [
                {"name": k, "value": v} for k, v in template_params.items()
            ]
        entries.append(entry)
    do_send_entries(entries, message, force=force)


def do_send_template(number: str, template_name: str, template_params: dict | None = None,
                     force: bool = False) -> None:
    """Send an approved WhatsApp template to a number (no 24h session needed)."""
    payload = {
        "channels": [
            {
                "channel": "whatsapp",
                "contact": number,
                "template_name": template_name,
                "template_language": os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en"),
            }
        ],
        "message": f"[template:{template_name}]",
    }
    if force:
        payload["resend"] = True
    if template_params:
        payload["channels"][0]["template_params"] = [
            {"name": k, "value": v} for k, v in template_params.items()
        ]
    code, body = http("POST", "/api/v1/notifications/send", payload)
    if body.get("duplicate") and not force:
        print(body.get("message") or "This message was already sent. Do you want to resend?")
        print(f"Existing message id: {body.get('message_id', '?')} (status: {body.get('status', '?')})")
        try:
            answer = input("Do you want to resend? (y/n): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer == "y":
            payload["resend"] = True
            code, body = http("POST", "/api/v1/notifications/send", payload)
        else:
            return
    if code != 202 or not body.get("message_id"):
        detail = body.get("error", body.get("detail", body))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        print(f"Error: {detail}")
        sys.exit(1)
    group_id = body["message_id"]
    for c in body.get("channels", []):
        print(f"  - {c['channel']} -> {c['contact']}  (message id: {c['message_id']}, template={template_name})")
    print(f"Group id    : {group_id}")
    print(f"Check status: python3 notification_service.py status {group_id}")
    if VERBOSE:
        print(json.dumps(body, indent=2))
    first_id = body["channels"][0]["message_id"] if body.get("channels") else group_id
    _print_recent_logs(notification_id=first_id, group_id=group_id)


def do_send_event(event_file: str) -> None:
    path = Path(event_file)
    if not path.is_file():
        print(f"Error: file not found: {event_file}", file=sys.stderr)
        sys.exit(1)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error: could not read {event_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    code, body = http("POST", "/api/v1/notifications/event", payload)
    if code != 202 or not body.get("message_id"):
        detail = body.get("error", body.get("detail", body))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        print(f"Error: {detail}")
        sys.exit(1)
    group_id = body["message_id"]
    print(f"Queued {len(body.get('channels', []))} delivery(ies):")
    for c in body.get("channels", []):
        print(f"  - {c['channel']} -> {c['contact']}  (message id: {c['message_id']})")
    print(f"Group id    : {group_id}")
    print(f"Check status: python3 notification_service.py status {group_id}")
    if VERBOSE:
        print(json.dumps(body, indent=2))
    first_id = body["channels"][0]["message_id"] if body.get("channels") else group_id
    _print_recent_logs(notification_id=first_id, group_id=group_id)


def do_status(message_id: str) -> None:
    code, body = http("GET", f"/api/v1/notifications/{message_id}/status")
    if code == 404:
        print(f"Message {message_id} not found.")
        sys.exit(1)
    if code != 200:
        detail = body.get("error", body.get("detail", body))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        print(f"Error: {detail}")
        sys.exit(1)
    marks = {"delivered": "DELIVERED", "failed": "FAILED",
             "sent": "SENT", "queued": "QUEUED", "partial": "PARTIAL"}
    print(f"Status: {marks.get(body.get('status', ''), body.get('status', ''))}")
    if body.get("reference"):
        print(f"reference    : {body['reference']}")
    for c in body.get("channels", []):
        mark = marks.get(c.get("status", ""), c.get("status", ""))
        print(f"  [{c.get('channel')}] {mark}")
        print(f"    contact      : {c.get('contact')}")
        print(f"    provider     : {c.get('provider')}")
        print(f"    error        : {c.get('error')}")
        print(f"    elapsed      : {c.get('elapsed_seconds')}s (timeout {c.get('delivery_timeout_seconds')}s)")
        if c.get("timed_out"):
            print("    TIMED OUT    : still waiting, no delivery receipt within SLA - check the provider/webhook")
        print(f"    updated at   : {c.get('updated_at')}")
    if VERBOSE:
        print(json.dumps(body, indent=2))


def interactive() -> None:
    ensure_server()
    if _SERVER_REQUIRES_AUTH:
        _ensure_api_key()  # prompt for the API key up front
    config_check()
    print("Notification Service CLI")
    print("------------------------")
    while True:
        choice = input("What do you want to do? (1=send 2=check status 3=quit): ").strip()
        if choice == "1":
            print("Channels: whatsapp, sms, email (comma-separated to send to several)")
            raw = input("Channel(s): ").strip()
            channels = [c.strip() for c in raw.split(",") if c.strip()]
            contact = input("Contact (phone or email): ").strip()
            message = input("Message: ").strip()
            entries = []
            for ch in channels:
                entry = {"channel": ch, "contact": contact}
                if ch == "whatsapp":
                    print("WhatsApp template: blank = none (free text, 24h window only) | or an approved Meta template name")
                    tpl = input("WhatsApp template name (blank = none): ").strip()
                    if tpl:
                        entry["template_name"] = tpl
                        raw_params = input(
                            "Template params (comma-separated name=value, blank = none): "
                        ).strip()
                        if raw_params:
                            entry["template_params"] = []
                            for pair in raw_params.split(","):
                                if "=" in pair:
                                    k, v = pair.split("=", 1)
                                    entry["template_params"].append({"name": k.strip(), "value": v.strip()})
                elif ch == "email":
                    print("Email templates: default | Add your own under templates/email/<name>.html")
                    tpl = input("Email template name (blank = default): ").strip()
                    if tpl:
                        entry["template_name"] = tpl
                # sms: no template support
                entries.append(entry)
            do_send_entries(entries, message)
            print()
        elif choice == "2":
            mid = input("Message id: ").strip()
            do_status(mid)
            print()
        elif choice == "3":
            print("Bye!")
            return
        elif UUID_RE.match(choice):
            do_status(choice)
            print()
        else:
            print("Pick 1, 2 or 3 (or paste a message id).")


# ---------------------------------------------------------------- main

def usage() -> None:
    print(__doc__)
    print("  -v/--verbose  show the full API response")


def main() -> None:
    global VERBOSE
    args = [a for a in sys.argv[1:] if not (a == "-v" or a == "--verbose")]
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

    if "-h" in sys.argv or "--help" in sys.argv:
        usage()
        return

    if not args:
        interactive()
        return

    cmd, *rest = args
    if cmd == "send" and len(rest) >= 3:
        ensure_server()
        template = ""
        params: dict | None = None
        force = False
        filtered = list(rest)
        if "--resend" in filtered or "--force" in filtered or "--force-resend" in filtered:
            force = True
            for flag in ("--resend", "--force", "--force-resend"):
                while flag in filtered:
                    filtered.remove(flag)
        if "--template" in filtered:
            i = filtered.index("--template")
            if i + 1 < len(filtered):
                template = filtered[i + 1]
                del filtered[i:i + 2]
        if "--param" in filtered:
            params = {}
            i = filtered.index("--param")
            while i < len(filtered) and filtered[i] == "--param":
                if i + 1 < len(filtered) and "=" in filtered[i + 1]:
                    k, v = filtered[i + 1].split("=", 1)
                    params[k.strip()] = v.strip()
                    del filtered[i:i + 2]
                else:
                    break
        channels = [_normalize_channel(c) for c in filtered[0].split(",") if c.strip()]
        do_send([(c, filtered[1]) for c in channels], filtered[2], template=template, template_params=params, force=force)
    elif cmd in ("send-template", "send_template", "sendwhatsapptemplate") and len(rest) >= 2:
        ensure_server()
        number = rest[0]
        template_name = rest[1]
        force = "--resend" in rest or "--force" in rest or "--force-resend" in rest
        # optional --param name=value (repeatable)
        params: dict | None = None
        tail = [a for a in rest[2:] if a not in ("--resend", "--force", "--force-resend")]
        if "--param" in tail:
            params = {}
            i = tail.index("--param")
            while i < len(tail) and tail[i] == "--param":
                if i + 1 < len(tail) and "=" in tail[i + 1]:
                    k, v = tail[i + 1].split("=", 1)
                    params[k.strip()] = v.strip()
                    del tail[i:i + 2]
                else:
                    break
        do_send_template(number, template_name, template_params=params, force=force)
    elif cmd == "status" and rest:
        ensure_server()
        do_status(rest[0])
    elif cmd in ("send-event", "send_event") and rest:
        ensure_server()
        do_send_event(rest[0])
    elif cmd == "audit":
        do_audit(rest)
    elif cmd == "logs":
        do_logs()
    elif cmd in ("db-check", "dbcheck"):
        do_db_check()
    else:
        usage()
        sys.exit(1)


def do_db_check() -> None:
    """Check the configured database connection without exposing credentials.

    Reports: backend, host, connectivity. Never prints DATABASE_URL.
    """
    try:
        from app.config import get_settings
        from app.storage import Storage

        get_settings.cache_clear()
        s = get_settings()
        storage = Storage()
        host = storage._safe_host()
        backend = storage.backend
        print(f"Database backend : {backend}")
        print(f"Database host    : {host}")
        if backend == "sqlite":
            print(f"SQLite path      : {s.DATABASE_PATH}")
            import os

            print("Status           : OK" if os.path.exists(s.DATABASE_PATH)
                  else "Status           : file not found yet")
            return
        storage.connect()
        storage.close()
        print("Status           : OK")
    except Exception as exc:  # noqa: BLE001
        print(f"Status           : FAILED ({exc})")
        sys.exit(1)


def do_audit(rest: list) -> None:
    """Show recent durable audit records (who/what/when/which notification/result).

    Reads from the dedicated AUDIT storage (DB `audit_logs` table, and the
    audit file when configured) — NOT from the application log.

    Usage:
        python3 notification_service.py audit
        python3 notification_service.py audit --limit 20
        python3 notification_service.py audit --user <user_id>
        python3 notification_service.py audit --file      # read from audit file
    """
    limit = 50
    user_id = None
    from_file = False
    for i, a in enumerate(rest):
        if a == "--limit" and i + 1 < len(rest):
            try:
                limit = int(rest[i + 1])
            except ValueError:
                limit = 50
        if a == "--user" and i + 1 < len(rest):
            user_id = rest[i + 1]
        if a in ("--file", "-f"):
            from_file = True

    app_log, audit_file = _log_paths()
    if audit_file:
        print(f"Audit file   : {audit_file}")

    if from_file:
        try:
            from app.config import get_settings
            from app.audit import list_audit_from_file

            get_settings.cache_clear()
            rows = list_audit_from_file(limit=limit)
        except Exception as exc:  # noqa: BLE001
            print(f"Error reading audit file: {exc}")
            sys.exit(1)
    else:
        try:
            from app.config import get_settings
            from app.audit import list_audit

            get_settings.cache_clear()
            rows = list_audit(limit=limit, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            print(f"Error reading audit log: {exc}")
            sys.exit(1)
    if not rows:
        print("No audit records found.")
        return
    print(f"Audit log (last {len(rows)} records):")
    for r in rows:
        ts = str(r.get("timestamp", ""))[:19]
        print(
            f"  {ts}  {r.get('action',''):<30} user={r.get('user_id','-')} "
            f"notif={r.get('notification_id','-')[:8]} channel={r.get('channel','-')} "
            f"status={r.get('status','-')} result={r.get('result','-')}"
        )


_LOG_COLORS = {
    "DEBUG": "[36m",
    "INFO": "[32m",
    "WARNING": "[33m",
    "ERROR": "[31m",
    "CRITICAL": "[35m[1m",
}
# Levels that colour the ENTIRE line so problems stand out in `logs` output.
_LOG_LINE_COLORS = {
    "WARNING": "[33m",
    "ERROR": "[31m",
    "CRITICAL": "[35m[1m",
}
_LOG_RESET = "[0m"


def _colorize_log_line(line: str) -> str:
    """Colour a log line read from the file for terminal display.

    Warning/error/critical lines are coloured entirely (yellow/red/magenta)
    so they stand out; other levels just colour the level token.
    """
    for tok, code in _LOG_LINE_COLORS.items():
        needle = f" {tok} "
        if needle in line or line.startswith(tok + " "):
            return f"{code}{line}{_LOG_RESET}"
    for tok, code in _LOG_COLORS.items():
        needle = f" {tok} "
        if needle in line:
            return line.replace(needle, f" {code}{tok}{_LOG_RESET} ", 1)
        if line.startswith(tok + " "):
            return line.replace(tok, f"{code}{tok}{_LOG_RESET}", 1)
    return line


def _log_level_filter(level: int) -> callable:
    """Return a predicate(line)->bool matching the terminal level filter.

    LOG_LEVEL=INFO shows ONLY INFO lines; DEBUG shows DEBUG+INFO; etc.
    """
    allowed = {
        logging.DEBUG: {"DEBUG", "INFO"},
        logging.INFO: {"INFO"},
        logging.WARNING: {"WARNING", "ERROR"},
        logging.ERROR: {"ERROR", "CRITICAL"},
        logging.CRITICAL: {"CRITICAL"},
    }.get(level, {"INFO"})

    def _match(line: str) -> bool:
        for tok in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            if f" {tok} " in line or line.startswith(tok + " "):
                return tok in allowed
        return False

    return _match


def do_logs() -> None:
    """Show the application log file filtered by LOG_LEVEL (default INFO-only).

    Usage:
        python3 notification_service.py logs                 # last 50 lines, INFO only
        python3 notification_service.py logs -f              # follow (tail -f)
        python3 notification_service.py logs --level ERROR   # only ERROR+CRITICAL
        python3 notification_service.py logs --all           # all levels
    """
    import logging as _logging

    follow = "-f" in sys.argv or "--follow" in sys.argv
    show_all = "--all" in sys.argv
    # Priority: --level CLI flag > LOG_LEVEL from settings > defaults
    if "--level" in sys.argv:
        i = sys.argv.index("--level")
        if i + 1 < len(sys.argv):
            level_name = sys.argv[i + 1].upper()
        else:
            level_name = "INFO"
    else:
        try:
            from app.config import get_settings

            get_settings.cache_clear()
            level_name = (get_settings().LOG_LEVEL or "INFO").upper()
        except Exception:  # noqa: BLE001
            level_name = "INFO"
    try:
        level = getattr(_logging, level_name, _logging.INFO)
    except Exception:  # noqa: BLE001
        level = _logging.INFO

    app_log, _ = _log_paths()
    if not os.path.exists(app_log):
        print(f"No application log yet at {app_log}. Start the server first.")
        return
    matcher = (lambda line: True) if show_all else _log_level_filter(level)
    print(f"Application log: {app_log}  (level={level_name})")
    try:
        if follow:
            # stream new lines like tail -f
            with open(app_log, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, 2)
                while True:
                    line = fh.readline()
                    if line:
                        if matcher(line):
                            print(_colorize_log_line(line), end="")
                    else:
                        time.sleep(0.5)
        else:
            with open(app_log, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            for line in lines[-50:]:
                if matcher(line):
                    print(_colorize_log_line(line), end="")
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
