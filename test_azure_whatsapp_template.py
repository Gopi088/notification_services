"""
Direct Azure WhatsApp TEMPLATE test - bypasses the app entirely.

Sends the approved template `test_template` (language `en`) to any number.
Templates work WITHOUT a 24h session, so they can reach new numbers.

Run from the project root (10-digit Indian number, or E.164):
    python3 test_azure_whatsapp_template.py                    # default +919887270348
    python3 test_azure_whatsapp_template.py 9887270348
    python3 test_azure_whatsapp_template.py +15551234567

The recipient can also be set via TEST_WHATSAPP_TO in the environment.

Never prints the connection string or access key.
"""
import os
import sys

RECIPIENT = os.environ.get(
    "TEST_WHATSAPP_TO",
    sys.argv[1] if len(sys.argv) > 1 else "+919887270348",
)
# Normalize to E.164 (e.g. 9887270348 -> +919887270348, 6362490250 -> +916362490250)
_raw = RECIPIENT
_digits = "".join(c for c in _raw if c.isdigit())
if _digits.startswith("91") and len(_digits) == 12:
    RECIPIENT = f"+{_digits}"
elif len(_digits) == 10:
    RECIPIENT = f"+91{_digits}"
elif _digits.startswith("+"):
    RECIPIENT = f"+{_digits.lstrip('+')}"
else:
    RECIPIENT = f"+{_digits}"

TEMPLATE_NAME = "test_template"
TEMPLATE_LANGUAGE = "en"
# If the template has variables, provide them here, e.g.
# {"name": "Rahul", "date": "25 August 2026", "position": "Software Engineer"}.
# test_template (as configured) has a static body with no variables.
TEMPLATE_PARAMS: dict | None = None


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
    connection_string = env.get("COMMUNICATION_SERVICES_CONNECTION_STRING") or env.get(
        "AZURE_COMMUNICATION_CONNECTION_STRING"
    )
    channel_id = env.get("WHATSAPP_CHANNEL_ID") or env.get("AZURE_WHATSAPP_CHANNEL_ID")
    sender = env.get("WHATSAPP_FROM") or env.get("AZURE_WHATSAPP_FROM") or "channel-linked number"

    if not connection_string or "your_" in connection_string:
        print("ERROR: COMMUNICATION_SERVICES_CONNECTION_STRING is not set in .env", file=sys.stderr)
        return 1
    if not channel_id:
        print("ERROR: WHATSAPP_CHANNEL_ID is not set in .env", file=sys.stderr)
        return 1

    print(f"[WhatsApp] Provider: Azure Communication Services")
    print(f"[WhatsApp] From: {sender}")
    print(f"[WhatsApp] To: {RECIPIENT}")
    print(f"[WhatsApp] Channel: {channel_id}")
    print(f"[WhatsApp] Mode: REAL")
    print(f"[WhatsApp] Message type: TEMPLATE")
    print(f"[WhatsApp] Template: {TEMPLATE_NAME}")
    print(f"[WhatsApp] Language: {TEMPLATE_LANGUAGE}")
    if TEMPLATE_PARAMS:
        print(f"[WhatsApp] Template params: {list(TEMPLATE_PARAMS)}")
    print(f"[WhatsApp] Sending template message...")

    try:
        from azure.communication.messages import NotificationMessagesClient
        from azure.communication.messages.models import (
            MessageTemplate,
            MessageTemplateText,
            TemplateNotificationContent,
            WhatsAppMessageTemplateBindings,
            WhatsAppMessageTemplateBindingsComponent,
        )
    except ImportError:
        print(
            "ERROR: azure-communication-messages is not installed.\n"
            "Install with:\n"
            "  venv/bin/pip install azure-communication-messages",
            file=sys.stderr,
        )
        return 1

    try:
        client = NotificationMessagesClient.from_connection_string(connection_string)

        if TEMPLATE_PARAMS:
            body_bindings = [
                WhatsAppMessageTemplateBindingsComponent(ref_value=name) for name in TEMPLATE_PARAMS
            ]
            template_values = [
                MessageTemplateText(name=name, text=str(value))
                for name, value in TEMPLATE_PARAMS.items()
            ]
            bindings = WhatsAppMessageTemplateBindings(body=body_bindings)
        else:
            bindings = WhatsAppMessageTemplateBindings(body=[])
            template_values = []

        template = MessageTemplate(
            name=TEMPLATE_NAME,
            language=TEMPLATE_LANGUAGE,
            bindings=bindings,
            template_values=template_values,
        )
        content = TemplateNotificationContent(
            channel_registration_id=channel_id,
            to=[RECIPIENT],
            template=template,
        )
        response = client.send(content)
    except Exception as exc:  # noqa: BLE001 - show the exact Azure error
        print(f"[WhatsApp] Azure REJECTED the template: {exc}", file=sys.stderr)
        return 1

    if not response.receipts:
        print("[WhatsApp] Azure returned NO delivery receipt", file=sys.stderr)
        return 1

    receipt = response.receipts[0]
    if getattr(receipt, "error", None):
        print(f"[WhatsApp] Azure returned an error for {receipt.to}: {receipt.error}", file=sys.stderr)
        return 1

    print(f"[WhatsApp] Azure message ID: {receipt.message_id}")
    print("[WhatsApp] Provider accepted message (delivery status comes via webhook)")
    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
