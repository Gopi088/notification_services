#!/usr/bin/env python3
"""
Notification Service CLI - run it like any Linux command.

  python3 notification_service.py                  interactive menu
  python3 notification_service.py send whatsapp 9887270348 "Hello"
  python3 notification_service.py send sms     9887270348 "Your OTP is 482913"
  python3 notification_service.py send email   you@example.com "Order shipped"
  python3 notification_service.py status <message_id>
  python3 notification_service.py -v status <message_id>

The server is started automatically if it isn't running.
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

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------- helpers

def http(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
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

    conn = env.get("AZURE_COMMUNICATION_CONNECTION_STRING", "")
    if not conn or "your_" in conn:
        print("! MOCK_MODE=false but AZURE_COMMUNICATION_CONNECTION_STRING is")
        print("  missing/placeholder in .env. Real sends will fail until you add it")
        print("  (Azure portal -> Communication Services -> Keys -> Connection string).")
        print()
        return
    if not env.get("AZURE_SMS_FROM"):
        print("  (sms) AZURE_SMS_FROM is empty - SMS sends will fail")
    if not env.get("AZURE_EMAIL_FROM"):
        print("  (email) AZURE_EMAIL_FROM is empty - Email sends will fail")
    if not env.get("AZURE_WHATSAPP_CHANNEL_ID"):
        print("  (whatsapp) AZURE_WHATSAPP_CHANNEL_ID is empty - WhatsApp sends will fail")
    print()
    print("  Note: WhatsApp outbound to a new contact needs an approved Meta")
    print("  template; free text only works inside a 24h session window.")
    print()


# ---------------------------------------------------------------- actions

def do_send(channel: str, contact: str, message: str) -> None:
    code, body = http("POST", "/send", {
        "channel": channel, "contact": contact, "message": message})
    if code != 202 or not body.get("message_id"):
        detail = body.get("detail", body)
        print(f"Error: {detail}")
        sys.exit(1)
    mid = body["message_id"]
    print(f"Queued for {channel} -> {contact}")
    print(f"Message id  : {mid}")
    print(f"Check status: python3 notification_service.py status {mid}")
    if VERBOSE:
        print(json.dumps(body, indent=2))


def do_status(message_id: str) -> None:
    code, body = http("GET", f"/status/{message_id}")
    if code == 404:
        print(f"Message {message_id} not found.")
        sys.exit(1)
    if code != 200:
        print(f"Error: {body.get('detail', body)}")
        sys.exit(1)
    status = body.get("status", "")
    marks = {"delivered": "DELIVERED", "failed": "FAILED",
             "sent": "SENT", "queued": "QUEUED"}
    print(f"Status: {marks.get(status, status)}")
    print(f"channel      : {body.get('channel')}")
    print(f"contact      : {body.get('contact')}")
    print(f"provider     : {body.get('provider')}")
    print(f"error        : {body.get('error')}")
    print(f"updated at   : {body.get('updated_at')}")
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
            channel = input("Channel (whatsapp/sms/email): ").strip()
            contact = input("Contact (phone or email): ").strip()
            message = input("Message: ").strip()
            do_send(channel, contact, message)
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
        do_send(rest[0], rest[1], rest[2])
    elif cmd == "status" and rest:
        ensure_server()
        do_status(rest[0])
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()