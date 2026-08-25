"""
Channel-aware message formatting + external template support.

Routes a single logical message into the format each channel expects:

- SMS      : plain text, optionally rendered through an SMS template
             (templates/sms/<name>.txt with {{var}} placeholders).
- WhatsApp : Meta-approved template (name + language + params) when provided,
             else free-form text (24h session only).
- Email    : subject + HTML body, optionally through an HTML email template
             (templates/email/<name>.html with {{subject}}/{{body}}).

A caller may also pass `template_name`/`template_params` for any channel; each
channel renders them through its own template mechanism. This keeps the core
orchestrator/provider layer free of channel-specific formatting logic.
"""
import re
from typing import Dict, Optional

from app.templates import TemplateError, render_email, render_sms_template

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class MessageFormatError(Exception):
    """Raised when a message cannot be formatted for a channel."""


def render_template_text(template: str, params: Optional[Dict[str, str]]) -> str:
    """Fill {{name}} placeholders in an arbitrary template string."""
    values = {k: str(v) for k, v in (params or {}).items()}
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), template)


def format_sms(message: str, template_name: Optional[str] = None,
               template_params: Optional[Dict[str, str]] = None) -> str:
    """Return the final SMS text (plain or template-rendered)."""
    if template_name:
        return render_sms_template(message, template_name, template_params)
    return message


def format_whatsapp(message: str, template_name: Optional[str] = None,
                    template_language: Optional[str] = None,
                    template_params: Optional[Dict[str, str]] = None) -> Dict:
    """Return a WhatsApp send descriptor.

    When a template is provided, returns {"template": name, "language": ...,
    "params": {...}}; otherwise returns {"text": message}.
    """
    if template_name:
        return {
            "template": template_name,
            "language": template_language,
            "params": template_params or {},
        }
    return {"text": message}


def format_email(message: str, subject: Optional[str] = None,
                 template_name: Optional[str] = None,
                 template_params: Optional[Dict[str, str]] = None) -> Dict:
    """Return an email content dict with subject + html body."""
    params = dict(template_params or {})
    subj = params.pop("subject", None) or subject or "Notification"
    try:
        html = render_email(body=message, subject=subj, template_name=template_name)
    except TemplateError as exc:
        raise MessageFormatError(str(exc)) from exc
    return {"subject": subj, "html": html}


def format_for_channel(
    channel: str,
    message: str,
    template_name: Optional[str] = None,
    template_language: Optional[str] = None,
    template_params: Optional[Dict[str, str]] = None,
):
    """Route one message into the correct per-channel format."""
    channel = channel.lower()
    if channel == "sms":
        return format_sms(message, template_name, template_params)
    if channel == "whatsapp":
        return format_whatsapp(message, template_name, template_language, template_params)
    if channel == "email":
        return format_email(message, template_params=template_params,
                            template_name=template_name)
    raise MessageFormatError(f"Unsupported channel for formatting: {channel}")
