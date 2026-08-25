"""Tests for rate limiting and validation."""
import pytest


@pytest.fixture()
def fake_redis_client(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    import app.ratelimit as ratelimit

    monkeypatch.setattr(ratelimit, "_redis", lambda: r)
    return r


def test_ratelimit_allowed_then_limited(fake_redis_client):
    from app import ratelimit

    rl = ratelimit.check_api_send("key-1")
    assert rl.allowed is True


def test_ratelimit_disabled_allows():
    from app import ratelimit

    r = ratelimit._check("rl:key:x:send", 0, 60)  # limit 0 -> allowed
    assert r.allowed is True


def test_validate_contact_phone():
    from app.schemas import Channel
    from app.validation import validate_contact

    validate_contact(Channel.sms, "9887270348")
    validate_contact(Channel.whatsapp, "+919887270348")


def test_validate_contact_invalid_phone():
    from app.schemas import Channel
    from app.validation import ContactValidationError, validate_contact

    with pytest.raises(ContactValidationError):
        validate_contact(Channel.sms, "abc")
    with pytest.raises(ContactValidationError):
        validate_contact(Channel.whatsapp, "123")


def test_validate_contact_email():
    from app.schemas import Channel
    from app.validation import validate_contact

    validate_contact(Channel.email, "a@b.com")


def test_validate_contact_invalid_email():
    from app.schemas import Channel
    from app.validation import ContactValidationError, validate_contact

    with pytest.raises(ContactValidationError):
        validate_contact(Channel.email, "not-an-email")
