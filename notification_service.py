#!/usr/bin/env python3
"""
Notification Service CLI - run it like any Linux command.

  python3 notification_service.py                  interactive menu
  python3 notification_service.py send whatsapp 9887270348 "Hello"
  python3 notification_service.py send sms     9887270348 "Your OTP is 482913"
  python3 notification_service.py send email   you@example.com "Order shipped"
  python3 notification_service.py send email,whatsapp +919887270348 "Order shipped"
  python3 notification_service.py send email you@example.com "Order shipped" --template default
  python3 notification_service.py send-template <number> <template_name> [--param name=value ...]
  python3 notification_service.py send-event event.json
  python3 notification_service.py status <message_id>
  python3 notification_service.py -v status <message_id>

The server is started automatically if it isn't running.
Set NOTIFICATION_API_KEY=<key> when AUTH_ENABLED=true in .env.
"""
import json
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
VERBOSE = False
API_KEY = os.environ.get("NOTIFICATION_API_KEY", "")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------- helpers

def http(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
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
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {"detail": str(exc)}


def server_up() -> bool:
    try:
        code, _ = http("GET", "/health")
        return code == 200
    except Exception:
        return False


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
    log = open(LOG_FILE, "ab")
    cmd = [str(ROOT / "run.sh")]
    if not (ROOT / "run.sh").exists():
        cmd = [str(ROOT / "venv" / "bin" / "uvicorn"), "app.main:app",
               "--host", "127.0.0.1", "--port", str(PORT)]
    subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)

    for _ in range(30):
        time.sleep(1)
        if server_up():
            print(f"Server ready at {BASE_URL}")
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
    if not conn or "your_" in conn:
        print("! MOCK_MODE=false but COMMUNICATION_SERVICES_CONNECTION_STRING is")
        print("  missing/placeholder in .env. Real sends will fail until you add it")
        print("  (Azure portal -> Communication Services -> Keys -> Connection string).")
        print()
        return
    if not env.get("AZURE_SMS_FROM"):
        print("  (sms) AZURE_SMS_FROM is empty - SMS sends will fail")
    if not env.get("AZURE_EMAIL_FROM"):
        print("  (email) AZURE_EMAIL_FROM is empty - Email sends will fail")
    if not (env.get("WHATSAPP_CHANNEL_ID") or env.get("AZURE_WHATSAPP_CHANNEL_ID")):
        print("  (whatsapp) WHATSAPP_CHANNEL_ID is empty - WhatsApp sends will fail")
    print()
    print("  Note: WhatsApp outbound to a new contact needs an approved Meta")
    print("  template; free text only works inside a 24h session window.")
    print()


# ---------------------------------------------------------------- actions

def do_send_entries(entries: list, message: str) -> None:
    """POST pre-built channel entries (each with its own channel/contact/template)."""
    payload = {"channels": entries, "message": message}
    code, body = http("POST", "/api/v1/notifications/send", payload)
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


def do_send(channels: list, message: str, template: str = "", template_params: dict | None = None) -> None:
    entries = []
    for ch, ct in channels:
        entry = {"channel": ch, "contact": ct}
        if template:
            entry["template_name"] = template
        if template_params:
            entry["template_params"] = [
                {"name": k, "value": v} for k, v in template_params.items()
            ]
        entries.append(entry)
    do_send_entries(entries, message)


def do_send_template(number: str, template_name: str, template_params: dict | None = None) -> None:
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
    if template_params:
        payload["channels"][0]["template_params"] = [
            {"name": k, "value": v} for k, v in template_params.items()
        ]
    code, body = http("POST", "/api/v1/notifications/send", payload)
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
        filtered = list(rest)
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
        channels = [c.strip() for c in filtered[0].split(",") if c.strip()]
        do_send([(c, filtered[1]) for c in channels], filtered[2], template=template, template_params=params)
    elif cmd in ("send-template", "send_template", "sendwhatsapptemplate") and len(rest) >= 2:
        ensure_server()
        number = rest[0]
        template_name = rest[1]
        # optional --param name=value (repeatable)
        params: dict | None = None
        tail = list(rest[2:])
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
        do_send_template(number, template_name, template_params=params)
    elif cmd == "status" and rest:
        ensure_server()
        do_status(rest[0])
    elif cmd in ("send-event", "send_event") and rest:
        ensure_server()
        do_send_event(rest[0])
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()