"""
Channel template rendering.

Email templates are HTML files under `templates/email/<name>.html` and may use
the `{{subject}}` and `{{body}}` placeholders. Example:

    templates/email/default.html
        <h1>{{subject}}</h1>
        <p>{{body}}</p>

WhatsApp templates are Meta-approved templates referenced by name via the
WhatsApp Business Manager; the name is passed through unchanged.
"""
import html
import os
import re
from pathlib import Path
from typing import Dict, Optional

from app.config import get_settings

_APP_DIR = Path(__file__).resolve().parent.parent
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class TemplateError(Exception):
    """Raised when a requested template cannot be found or rendered."""


def _templates_dir() -> Path:
    settings = get_settings()
    configured = Path(settings.TEMPLATES_DIR)
    if configured.is_absolute():
        return configured
    return _APP_DIR / configured


def _email_template_path(name: str) -> Path:
    # Never allow path traversal outside the templates dir.
    safe_name = Path(name).name
    path = _templates_dir() / "email" / f"{safe_name}.html"
    if not path.is_file():
        available = sorted(p.name for p in (_templates_dir() / "email").glob("*.html"))
        raise TemplateError(
            f"Email template '{safe_name}' not found. Available templates: {available or 'none'}."
        )
    return path


def render_email(
    body: str,
    subject: str = "Notification",
    template_name: Optional[str] = None,
) -> str:
    """
    Render the HTML email body using a template from templates/email/.
    Falls back to the configured default template when template_name is None.
    """
    settings = get_settings()
    name = template_name or settings.EMAIL_TEMPLATE_NAME
    try:
        raw = _email_template_path(name).read_text(encoding="utf-8")
    except TemplateError:
        # If even the default is missing, render a plain safe fallback.
        if name == settings.EMAIL_TEMPLATE_NAME:
            return f"<p>{html.escape(body)}</p>"
        raise

    values: Dict[str, str] = {
        "subject": html.escape(subject),
        "body": html.escape(body),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), raw)


def render_sms(message: str) -> str:
    """SMS has no formatting; return the message unchanged."""
    return message


def render_sms_template(
    message: str,
    template_name: str,
    params: Optional[Dict[str, str]] = None,
) -> str:
    """
    Render an SMS through a plain-text template from templates/sms/<name>.txt.

    The template may contain {{body}} (the original message) and any number of
    {{name}} placeholders filled from `params`. Missing templates fall back to
    the plain message so SMS sends never break on a missing file.
    """
    values: Dict[str, str] = {
        "body": message,
        **( {str(k): str(v) for k, v in (params or {}).items()}),
    }
    path = _templates_dir() / "sms" / f"{Path(template_name).name}.txt"
    if not path.is_file():
        return message
    raw = path.read_text(encoding="utf-8")
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), raw)
