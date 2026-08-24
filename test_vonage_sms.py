"""
Direct Vonage SMS test - bypasses the app entirely.

Sends "Hello from Vonage" to any number via the Vonage Messages API.

Run from the project root (10-digit Indian number, or E.164):
    python3 test_vonage_sms.py                    # default +919887270348
    python3 test_vonage_sms.py 9887270348
    python3 test_vonage_sms.py +15551234567

Never prints the API secret.
"""
import sys

from vonage import Auth, Vonage
from vonage_messages import Sms

DEFAULT_RECIPIENT = "+919887270348"
MESSAGE = "Hello from Vonage"


def _normalize_phone(contact: str) -> str:
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
    api_key = env.get("VONAGE_API_KEY", "")
    api_secret = env.get("VONAGE_API_SECRET", "")
    from_sender = env.get("VONAGE_SMS_FROM", "Vonage APIs")

    # Accept target number from CLI arg (any format: 9887270348, +919887270348, etc.)
    raw_recipient = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RECIPIENT
    recipient = _normalize_phone(raw_recipient)
    to_digits = recipient.lstrip("+")

    if not api_key or not api_secret:
        print("ERROR: VONAGE_API_KEY and VONAGE_API_SECRET must be set in .env", file=sys.stderr)
        return 1

    print(f"[SMS] Provider: Vonage")
    print(f"[SMS] From: {from_sender}")
    print(f"[SMS] To: {recipient}")
    print(f"[SMS] Mode: REAL")
    print(f"[SMS] Sending text message...")

    try:
        client = Vonage(Auth(api_key=api_key, api_secret=api_secret))
        response = client.messages.send(
            Sms(to=to_digits, from_=from_sender, text=MESSAGE)
        )
    except Exception as exc:  # noqa: BLE001 - show the exact Vonage error
        print(f"[SMS] Vonage REJECTED the message: {exc}", file=sys.stderr)
        return 1

    message_id = response.message_uuid if hasattr(response, "message_uuid") else (
        response.get("message_uuid") if isinstance(response, dict) else None
    )
    if not message_id:
        print(f"[SMS] Vonage returned no message id: {response}", file=sys.stderr)
        return 1

    print(f"[SMS] Vonage message ID: {message_id}")
    print(f"[SMS] Provider accepted message")
    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
