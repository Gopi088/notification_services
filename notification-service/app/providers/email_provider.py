"""
Email provider backed by plain SMTP (works with Gmail, SES SMTP, Mailgun
SMTP, Outlook365, or any standard SMTP relay).

Requires SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL.
"""
import smtplib
import uuid
from email.mime.text import MIMEText

from app.config import get_settings
from app.providers.base import NotificationProvider, ProviderConfigError, ProviderError, ProviderResult


class EmailProvider(NotificationProvider):
    name = "smtp_email"

    def __init__(self):
        self.settings = get_settings()

    def send(self, contact: str, message: str) -> ProviderResult:
        if self.settings.MOCK_MODE:
            return ProviderResult(self.name, f"mock-{uuid.uuid4().hex[:12]}", "sent")

        s = self.settings
        if not (s.SMTP_HOST and s.SMTP_USERNAME and s.SMTP_PASSWORD and s.SMTP_FROM_EMAIL):
            raise ProviderConfigError(
                "Email provider is not configured. Set SMTP_HOST, SMTP_USERNAME, "
                "SMTP_PASSWORD and SMTP_FROM_EMAIL in .env."
            )

        mime_msg = MIMEText(message, "plain", "utf-8")
        mime_msg["Subject"] = "Notification"
        mime_msg["From"] = s.SMTP_FROM_EMAIL
        mime_msg["To"] = contact

        try:
            with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=15) as server:
                if s.SMTP_USE_TLS:
                    server.starttls()
                server.login(s.SMTP_USERNAME, s.SMTP_PASSWORD)
                server.sendmail(s.SMTP_FROM_EMAIL, [contact], mime_msg.as_string())
        except smtplib.SMTPException as exc:
            raise ProviderError(f"SMTP error: {exc}") from exc
        except OSError as exc:
            raise ProviderError(f"Network error contacting SMTP server: {exc}") from exc

        return ProviderResult(self.name, f"smtp-{uuid.uuid4().hex[:12]}", "sent")
