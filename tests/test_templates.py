"""Tests for template rendering."""
import pytest

from app.templates import TemplateError, render_email, render_sms


def test_render_sms_unchanged():
    assert render_sms("hello") == "hello"


def test_render_email_default_fallback():
    html = render_email(body="<script>alert(1)</script>", subject="Hi")
    # Missing default template -> plain escaped fallback
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_email_missing_named_template():
    with pytest.raises(TemplateError):
        render_email(body="x", subject="y", template_name="does-not-exist")


def test_render_email_path_traversal_blocked():
    with pytest.raises(TemplateError):
        render_email(body="x", subject="y", template_name="../../etc/passwd")


def test_render_email_escapes_values(tmp_path, monkeypatch):
    import os

    from app.templates import _APP_DIR, _templates_dir

    # Create a template dir with a custom template
    tdir = tmp_path / "email"
    tdir.mkdir(parents=True)
    (tdir / "custom.html").write_text("<h1>{{subject}}</h1><p>{{body}}</p>", encoding="utf-8")
    monkeypatch.setattr("app.templates._templates_dir", lambda: tmp_path)
    html = render_email(body="B <b>", subject="S & co", template_name="custom")
    assert "S &amp; co" in html
    assert "B &lt;b&gt;" in html
