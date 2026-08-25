"""
Tests for the Vonage WhatsApp Sandbox provider and endpoint integration.

Mocks the Vonage HTTP request so no real WhatsApp message is sent.

Run:
    python3 test_vonage_whatsapp.py       # no pytest required
    python3 -m pytest test_vonage_whatsapp.py -v
"""
import json as _json
import sys
from contextlib import contextmanager
from unittest.mock import patch

import requests


class _FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.text = body
        self._body = body

    def json(self):
        return _json.loads(self._body)


@contextmanager
def _test_env(**overrides):
    """Set env vars for the duration of a test and clear the settings cache."""
    from app.config import get_settings

    base = {
        "MOCK_MODE": "false",
        "RATELIMIT_ENABLED": "false",
        "VONAGE_API_KEY": "test-key",
        "VONAGE_API_SECRET": "test-secret",
        "VONAGE_WHATSAPP_FROM": "14157386102",
        "VONAGE_WHATSAPP_SANDBOX_URL": "https://messages-sandbox.nexmo.com/v1/messages",
        "AZURE_DEFAULT_COUNTRY_CODE": "91",
    }
    base.update(overrides)
    with patch.dict("os.environ", base):
        get_settings.cache_clear()
        yield


def test_success_sends_correct_payload(client=None):
    captured = {}

    def fake_post(url, auth=None, headers=None, body=None, timeout=None, **kw):
        captured["url"] = url
        captured["auth"] = auth
        captured["headers"] = headers
        captured["body"] = body if body else kw.get("json", {})
        captured["timeout"] = timeout
        return _FakeResponse(
            200, _json.dumps({"message_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})
        )

    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch("app.providers.vonage_provider.requests.post", side_effect=fake_post):
            result = provider.send("9887270348", "Hello from Notification API")

    assert captured["url"] == "https://messages-sandbox.nexmo.com/v1/messages"
    assert captured["auth"] == ("test-key", "test-secret")
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["timeout"] == 30

    body = captured["body"]
    assert body["from"] == "14157386102"
    assert body["to"] == "919887270348"
    assert body["message_type"] == "text"
    assert body["text"] == "Hello from Notification API"
    assert body["channel"] == "whatsapp"

    assert result.provider_name == "vonage_whatsapp"
    assert result.provider_message_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert result.status == "submitted"


def test_success_existing_number_with_plus(client=None):
    captured = {}

    def fake_post(url, **kw):
        captured["body"] = kw.get("json", {})
        return _FakeResponse(
            200, _json.dumps({"message_uuid": "11111111-2222-3333-4444-555555555555"})
        )

    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch("app.providers.vonage_provider.requests.post", side_effect=fake_post):
            provider.send("+919887270348", "hello")

    assert captured["body"]["to"] == "919887270348"


def test_missing_api_key(client=None):
    with _test_env(VONAGE_API_KEY=""):
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        try:
            provider.send("9887270348", "hello")
            assert False, "expected ProviderConfigError"
        except Exception as exc:  # noqa: BLE001
            assert "VONAGE_API_KEY" in str(exc)


def test_missing_api_secret(client=None):
    with _test_env(VONAGE_API_SECRET=""):
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        try:
            provider.send("9887270348", "hello")
            assert False, "expected ProviderConfigError"
        except Exception as exc:  # noqa: BLE001
            assert "VONAGE_API_SECRET" in str(exc)


def test_missing_whatsapp_from(client=None):
    with _test_env(VONAGE_WHATSAPP_FROM=""):
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        try:
            provider.send("9887270348", "hello")
            assert False, "expected ProviderConfigError"
        except Exception as exc:  # noqa: BLE001
            assert "VONAGE_WHATSAPP_FROM" in str(exc)


def test_http_401_auth_error(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            return_value=_FakeResponse(401, '{"title": "Unauthorised"}'),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "401" in str(exc)
                assert "authentication" in str(exc).lower()


def test_http_403_sandbox_not_allowlisted(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            return_value=_FakeResponse(403, "forbidden"),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "403" in str(exc)
                assert "allow-listed" in str(exc).lower()


def test_http_500_vonage_error(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            return_value=_FakeResponse(500, '{"title": "Internal Server Error"}'),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "500" in str(exc)


def test_network_error(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "network" in str(exc).lower()


def test_no_message_uuid_in_response(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            return_value=_FakeResponse(200, _json.dumps({"something": "else"})),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "no message id" in str(exc)


def test_secret_not_exposed_in_errors(client=None):
    with _test_env():
        from app.providers.vonage_provider import VonageWhatsAppProvider

        provider = VonageWhatsAppProvider()
        with patch(
            "app.providers.vonage_provider.requests.post",
            return_value=_FakeResponse(500, '{"title": "boom"}'),
        ):
            try:
                provider.send("9887270348", "hello")
                assert False, "expected ProviderError"
            except Exception as exc:  # noqa: BLE001
                assert "test-secret" not in str(exc)


def test_endpoint_whatsapp_regression(client=None):
    """POST /api/v1/notifications/send with channel=whatsapp reaches the provider."""
    import uuid

    from fastapi.testclient import TestClient
    from app.main import app

    with _test_env():
        import fakeredis
        server = fakeredis.FakeServer()
        fake_r = fakeredis.FakeRedis(server=server, decode_responses=True)
        import app.idempotency as idem_mod
        from unittest.mock import patch as _patch
        with _patch.object(idem_mod, "_redis", lambda: fake_r):
            with _patch("app.providers.vonage_provider.requests.post") as mock_post:
                mock_post.return_value = _FakeResponse(
                    200, _json.dumps({"message_uuid": "22222222-3333-4444-5555-666666666666"})
                )
                client = TestClient(app)
                resp = client.post(
                    "/api/v1/notifications/send",
                    json={
                        "channels": [{"channel": "whatsapp", "contact": "9887270348"}],
                        "message": f"hi-{uuid.uuid4().hex[:6]}",
                    },
                )
                assert resp.status_code == 202, resp.text
                body = resp.json()
                assert body["channels"][0]["channel"] == "whatsapp"
                assert mock_post.call_count >= 1


if __name__ == "__main__":
    tests = [
        ("success_sends_correct_payload", test_success_sends_correct_payload),
        ("success_existing_number_with_plus", test_success_existing_number_with_plus),
        ("missing_api_key", test_missing_api_key),
        ("missing_api_secret", test_missing_api_secret),
        ("missing_whatsapp_from", test_missing_whatsapp_from),
        ("http_401_auth_error", test_http_401_auth_error),
        ("http_403_sandbox_not_allowlisted", test_http_403_sandbox_not_allowlisted),
        ("http_500_vonage_error", test_http_500_vonage_error),
        ("network_error", test_network_error),
        ("no_message_uuid_in_response", test_no_message_uuid_in_response),
        ("secret_not_exposed_in_errors", test_secret_not_exposed_in_errors),
        ("endpoint_whatsapp_regression", test_endpoint_whatsapp_regression),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001 - test runner
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)