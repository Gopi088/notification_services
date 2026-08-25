"""
Test all notification channels at once.
Usage:
  1. Set MOCK_MODE=true in .env and restart the server (free dry-run).
  2. Set MOCK_MODE=false in .env and restart the server (real sends, uses credits).
  3. Run: python test_all_channels.py
"""
import requests
import time
import sys

BASE = "http://127.0.0.1:8000"

EMAIL = "test@example.com"
PHONE = "+918660556303"


def send_test():
    payload = {
        "channels": [
            {"channel": "email", "contact": EMAIL},
            {"channel": "sms", "contact": PHONE},
            {"channel": "whatsapp", "contact": PHONE},
        ],
        "message": "Test notification from notification_services",
    }
    print(f"POST {BASE}/api/v1/notifications/send")
    r = requests.post(f"{BASE}/api/v1/notifications/send", json=payload, timeout=10)
    print(f"  Status: {r.status_code}")
    data = r.json()
    print(f"  Response: {data}")
    return data


def poll_status(group_id: str, timeout: int = 30):
    print(f"\nPolling status for group {group_id} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/v1/notifications/{group_id}/status", timeout=10)
        data = r.json()
        statuses = [ch["status"] for ch in data.get("channels", [])]
        print(f"  Overall: {data.get('status')}  |  Per-channel: {statuses}")
        if data.get("status") in ("delivered", "failed"):
            print("\nFinal result:")
            for ch in data["channels"]:
                icon = "+" if ch["status"] == "delivered" else "x"
                err = f"  err={ch['error']}" if ch.get("error") else ""
                print(f"  [{icon}] {ch['channel']:10s} -> {ch['status']}{err}")
            return data
        time.sleep(2)
    print("  Timed out waiting for delivery.")
    return None


if __name__ == "__main__":
    try:
        resp = send_test()
    except requests.ConnectionError:
        print("ERROR: Cannot connect. Is the server running on http://127.0.0.1:8000 ?")
        sys.exit(1)

    if resp.get("status") == "queued":
        group_id = resp["message_id"]
        result = poll_status(group_id)
        if result and result["status"] == "delivered":
            print("\nAll channels working!")
        elif result and result["status"] == "failed":
            print("\nSome channels failed — check errors above.")
    else:
        print(f"\nUnexpected status: {resp.get('status')}")
