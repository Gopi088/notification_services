"""
Direct Twilio SMS test - bypasses the app entirely.

Sends an SMS to ANY number via the Twilio REST Messages API.

IMPORTANT: Twilio TRIAL accounts only accept predefined SMS templates, not
free-form text (error 572006 otherwise). The default body is the predefined
template name `sms_appointment_reminders`, which Twilio renders into the real
message. On a paid/verified account you can pass any free-form text instead.

Run from the project root (10-digit Indian number, or E.164):
    python3 test_twilio_sms.py                             # template sms_appointment_reminders
    python3 test_twilio_sms.py 9887270348
    python3 test_twilio_sms.py 9887270348 "Your message"   # paid/verified accounts
    python3 test_twilio_sms.py +15551234567 "Hi there"

The recipient/body can also be set via TEST_SMS_TO / TEST_SMS_BODY in the
environment. Credentials (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM)
come from `.env`.

Never prints the auth token.
"""
import os
import sys

import requests

DEFAULT_RECIPIENT = "+919148443937"
DEFAULT_MESSAGE = "sms_appointment_reminders"


def _normalize_phone(contact: str) -> str:
    """'9887270348' / '+15551234567' -> E.164 (defaults to +91 for 10-digit)."""
    digits = "".join(c for c in contact if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+91{digits}"
    return f"+{digits}"


def _load_env(path: str = ".env") -> dict:
    """Minimal .env reader with no external dependency."""
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                env[key.strip()] = val
    except FileNotFoundError:
        pass
    return env


def main() -> int:
    env = _load_env()
    sid = env.get("TWILIO_ACCOUNT_SID", "")
    token = env.get("TWILIO_AUTH_TOKEN", "")
    sender = env.get("TWILIO_FROM", "")

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    recipient = os.environ.get("TEST_SMS_TO", args[0] if args else DEFAULT_RECIPIENT)
    message = os.environ.get(
        "TEST_SMS_BODY", args[1] if len(args) > 1 else DEFAULT_MESSAGE
    )
    to = _normalize_phone(recipient)

    if not sid or not token:
        print(
            "ERROR: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env",
            file=sys.stderr,
        )
        return 1
    if not sender:
        print("ERROR: TWILIO_FROM must be set in .env", file=sys.stderr)
        return 1

    print("[SMS] Provider: Twilio")
    print(f"[SMS] From: {sender}")
    print(f"[SMS] To: {to}")
    print("[SMS] Mode: REAL")
    print("[SMS] Sending text message...")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        resp = requests.post(
            url,
            auth=(sid, token),
            data={"To": to, "From": sender, "Body": message},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - show the exact Twilio error
        print(f"[SMS] Twilio network error: {exc}", file=sys.stderr)
        return 1

    if resp.status_code not in (200, 201):
        print(f"[SMS] Twilio REJECTED the message ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    msg_sid = data.get("sid")
    if not msg_sid:
        print(f"[SMS] Twilio returned no sid: {data}", file=sys.stderr)
        return 1

    print(f"[SMS] Twilio message SID: {msg_sid}")
    print(f"[SMS] Provider accepted message (status: {data.get('status')})")
    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
