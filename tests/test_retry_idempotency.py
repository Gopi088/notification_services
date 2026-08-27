"""Tests for retry policy and idempotency helpers."""

from unittest.mock import patch

from app.retry import backoff_delay_ms, is_retryable_error
from app.providers.base import ProviderConfigError, ProviderError


def test_backoff_bounds():
    # attempt 1 -> ~base (5000) with jitter ±20%
    with patch("app.retry.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.RETRY_BASE_DELAY_MS = 5000
        s.RETRY_MAX_DELAY_MS = 120000
        s.RETRY_JITTER_RATIO = 0.2
        for attempt in (1, 2, 3, 4, 5):
            d = backoff_delay_ms(attempt)
            assert 0 < d <= 144000
        d6 = backoff_delay_ms(6)
        assert 0 < d6 <= 144000  # capped at max with jitter


def test_backoff_monotonic_on_average():
    # With jitter, individual samples can vary; check the base is monotonic
    # by sampling multiple times and taking the min.
    import random

    with patch("app.retry.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.RETRY_BASE_DELAY_MS = 5000
        s.RETRY_MAX_DELAY_MS = 120000
        s.RETRY_JITTER_RATIO = 0.0  # no jitter -> deterministic
        d1 = backoff_delay_ms(1)
        d2 = backoff_delay_ms(2)
        d3 = backoff_delay_ms(3)
        assert d1 == 5000
        assert d2 == 10000
        assert d3 == 20000


def test_retryable_error_classification():
    assert is_retryable_error(ProviderError("x", retryable=True)) is True
    assert is_retryable_error(ProviderError("x", retryable=False)) is False
    assert is_retryable_error(ProviderConfigError("config")) is False
    # generic exception -> retryable (best-effort)
    assert is_retryable_error(RuntimeError("boom")) is True


def test_derive_key_stable_and_distinct():
    from app.idempotency import derive_key

    k1 = derive_key("sms", "+919887270348", "hello", None)
    k2 = derive_key("sms", "+919887270348", "hello", None)
    k3 = derive_key("sms", "+919887270348", "hello", "ref")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64  # sha256 hex


def test_normalize_client_key():
    from app.idempotency import normalize_client_key

    assert normalize_client_key("  ABC123  ") == "abc123"
    assert normalize_client_key("Order-42") == "order-42"
    import pytest

    with pytest.raises(ValueError):
        normalize_client_key("x" * 200)
    with pytest.raises(ValueError):
        normalize_client_key("a\x00b")


def test_payload_hash_stable():
    from app.idempotency import payload_hash

    h1 = payload_hash({"channels": [{"channel": "sms"}], "message": "hi"})
    h2 = payload_hash({"message": "hi", "channels": [{"channel": "sms"}]})
    assert h1 == h2  # sort_keys -> order independent


def test_content_fingerprint_identifies_duplicate():
    from app.idempotency import content_fingerprint

    a = content_fingerprint("user1", "sms", "+919887270348", "hello")
    b = content_fingerprint("user1", "sms", "+919887270348", "hello")
    assert a == b  # identical content -> same fingerprint

    # Different user / channel / recipient / message all change the fingerprint.
    assert a != content_fingerprint("user2", "sms", "+919887270348", "hello")
    assert a != content_fingerprint("user1", "whatsapp", "+919887270348", "hello")
    assert a != content_fingerprint("user1", "sms", "+15551234567", "hello")
    assert a != content_fingerprint("user1", "sms", "+919887270348", "other")


def test_content_fingerprint_template_aware():
    from app.idempotency import content_fingerprint

    t1 = content_fingerprint("u", "whatsapp", "+919887270348", "free text",
                             template_name="test_template", template_params={"1": "Rahul"})
    t2 = content_fingerprint("u", "whatsapp", "+919887270348", "free text",
                             template_name="test_template", template_params={"1": "Rahul"})
    t3 = content_fingerprint("u", "whatsapp", "+919887270348", "free text",
                             template_name="test_template", template_params={"1": "Other"})
    free = content_fingerprint("u", "whatsapp", "+919887270348", "free text")
    assert t1 == t2
    assert t1 != t3  # different template params
    assert t1 != free  # template send != free-text send


def test_derive_key_includes_user():
    from app.idempotency import derive_key

    k1 = derive_key("sms", "+919887270348", "hello", None, user_id="user-a")
    k2 = derive_key("sms", "+919887270348", "hello", None, user_id="user-b")
    assert k1 != k2  # different users never share a derived key
