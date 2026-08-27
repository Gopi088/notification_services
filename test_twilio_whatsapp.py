"""
Direct Twilio WhatsApp test - bypasses the app entirely.

Sends a WhatsApp message to ANY number via the Twilio REST Messages API.

Free-form text only works inside a 24h session window (after the recipient
messages you first). For NEW numbers use --template (an approved content
template, no session required).

Run from the project root:
    python3 test_twilio_whatsapp.py 9887270348                    # free text (24h session)
    python3 test_twilio_whatsapp.py 9887270348 "Hello"            # custom body
    python3 test_twilio_whatsapp.py 9887270348 --template         # default TWILIO_WHATSAPP_CONTENT_SID
    python3 test_twilio_whatsapp.py 9887270348 --template test_template
    python3 test_twilio_whatsapp.py 9887270348 --template --param 1=Rahul
    python3 test_twilio_whatsapp.py +15551234567 --template

Credentials come from `.env` (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
TWILIO_WHATSAPP_FROM / TWILIO_FROM, TWILIO_WHATSAPP_CONTENT_SID).

Never prints the auth token.
"""
import json
import os
import sys

import requests

DEFAULT_RECIPIENT = "+919887270348"
DEFAULT_MESSAGE = "Hello from Twilio WhatsApp"


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


def _template_sid(env: dict, template_name: str) -> str:
    """Resolve a template name to a ContentSid (mapping -> default)."""
    mapping: dict = {}
    raw = env.get("TWILIO_WHATSAPP_TEMPLATES", "")
    for part in raw.replace(";", ",").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            mapping[k.strip()] = v.strip()
    if template_name:
        if template_name in mapping:
            return mapping[template_name]
        if not env.get("TWILIO_WHATSAPP_CONTENT_SID"):
            print(
                f"ERROR: no ContentSid for template '{template_name}' in "
                "TWILIO_WHATSAPP_TEMPLATES (and no TWILIO_WHATSAPP_CONTENT_SID)",
                file=sys.stderr,
            )
            sys.exit(1)
    if not env.get("TWILIO_WHATSAPP_CONTENT_SID"):
        print(
            "ERROR: TWILIO_WHATSAPP_CONTENT_SID must be set in .env for template sends",
            file=sys.stderr,
        )
        sys.exit(1)
    return env["TWILIO_WHATSAPP_CONTENT_SID"]


def main() -> int:
    env = _load_env()
    sid = env.get("TWILIO_ACCOUNT_SID", "")
    token = env.get("TWILIO_AUTH_TOKEN", "")
    sender = env.get("TWILIO_WHATSAPP_FROM") or env.get("TWILIO_FROM") or ""

    args = sys.argv[1:]
    use_template = "--template" in args
    rest = [a for a in args if a not in ("--template", "--param")]
    recipient = os.environ.get("TEST_WHATSAPP_TO", rest[0] if rest else DEFAULT_RECIPIENT)
    to = _normalize_phone(recipient)

    template_name = ""
    params: dict = {}
    if "--param" in args:
        i = args.index("--param")
        while i + 1 < len(args) and args[i + 1] != "--template":
            if "=" in args[i + 1]:
                k, _, v = args[i + 1].partition("=")
                params[k.strip()] = v.strip()
            i += 1

    if not sid or not token:
        print(
            "ERROR: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env",
            file=sys.stderr,
        )
        return 1
    if not sender:
        print(
            "ERROR: TWILIO_WHATSAPP_FROM (or TWILIO_FROM) must be set in .env",
            file=sys.stderr,
        )
        return 1

    print("[WhatsApp] Provider: Twilio")
    print(f"[WhatsApp] From: {sender}")
    print(f"[WhatsApp] To: {to}")
    print("[WhatsApp] Mode: REAL")
    if use_template:
        template_name = rest[1] if len(rest) > 1 else env.get("TWILIO_WHATSAPP_TEMPLATES", "").split("=")[0] or ""
        content_sid = _template_sid(env, template_name)
        print("[WhatsApp] Message type: TEMPLATE")
        print(f"[WhatsApp] ContentSid: {content_sid}")
        if params:
            print(f"[WhatsApp] ContentVariables: {params}")
        print("[WhatsApp] Sending template message...")
        data = {
            "To": f"whatsapp:{to}",
            "From": f"whatsapp:{sender}",
            "ContentSid": content_sid,
        }
        if params:
            data["ContentVariables"] = json.dumps(params)
    else:
        message = os.environ.get("TEST_WHATSAPP_BODY", rest[1] if len(rest) > 1 else DEFAULT_MESSAGE)
        print("[WhatsApp] Message type: TEXT (24h session window)")
        print("[WhatsApp] Sending text message...")
        data = {
            "To": f"whatsapp:{to}",
            "From": f"whatsapp:{sender}",
            "Body": message,
        }

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        resp = requests.post(url, auth=(sid, token), data=data, timeout=30)
    except Exception as exc:  # noqa: BLE001 - show the exact Twilio error
        print(f"[WhatsApp] Twilio network error: {exc}", file=sys.stderr)
        return 1

    if resp.status_code not in (200, 201):
        print(
            f"[WhatsApp] Twilio REJECTED the message ({resp.status_code}): {resp.text}",
            file=sys.stderr,
        )
        return 1

    result = resp.json()
    msg_sid = result.get("sid")
    if not msg_sid:
        print(f"[WhatsApp] Twilio returned no sid: {result}", file=sys.stderr)
        return 1

    print(f"[WhatsApp] Twilio message SID: {msg_sid}")
    print(f"[WhatsApp] Provider accepted message (status: {result.get('status')})")
    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
