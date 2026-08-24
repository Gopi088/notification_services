"""
Direct Azure WhatsApp test - bypasses the app entirely.

Sends "Hello from Azure Communication Services" to +919887270348 via the
configured Azure WhatsApp channel.

Run from the project root:
    python3 test_azure_whatsapp.py

Never prints the connection string or access key.
"""
import os
import re
import sys

RECIPIENT = "+919887270348"
MESSAGE = "Hello from Azure Communication Services"


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
    print(f"[WhatsApp] Message type: TEXT (24h session window)")
    print(f"[WhatsApp] Sending text message...")

    try:
        from azure.communication.messages import NotificationMessagesClient
        from azure.communication.messages.models import TextNotificationContent
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
        content = TextNotificationContent(
            channel_registration_id=channel_id,
            to=[RECIPIENT],
            content=MESSAGE,
        )
        response = client.send(content)
    except Exception as exc:  # noqa: BLE001 - show the exact Azure error
        print(f"[WhatsApp] Azure REJECTED the message: {exc}", file=sys.stderr)
        return 1

    if not response.receipts:
        print("[WhatsApp] Azure returned NO delivery receipt", file=sys.stderr)
        return 1

    receipt = response.receipts[0]
    if getattr(receipt, "error", None):
        print(f"[WhatsApp] Azure returned an error for {receipt.to}: {receipt.error}", file=sys.stderr)
        return 1

    print(f"[WhatsApp] Azure message ID: {receipt.message_id}")
    print(f"[WhatsApp] Provider accepted message")
    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())