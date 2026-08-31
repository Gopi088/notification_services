"""Direct unit coverage for local runtime configuration and lifecycle hooks."""
import pytest
from fastapi import HTTPException


def test_configuration_aliases_prefer_canonical_values():
    from app.config import Settings

    settings = Settings(
        COMMUNICATION_SERVICES_CONNECTION_STRING="canonical",
        AZURE_COMMUNICATION_CONNECTION_STRING="legacy",
        WHATSAPP_CHANNEL_ID="channel",
        AZURE_WHATSAPP_CHANNEL_ID="legacy-channel",
        WHATSAPP_FROM="+15550000001",
        AZURE_WHATSAPP_FROM="+15550000002",
        WHATSAPP_TEMPLATE_NAME="template",
        AZURE_WHATSAPP_TEMPLATE_NAME="legacy-template",
        WHATSAPP_TEMPLATE_LANGUAGE="en_GB",
        AZURE_WHATSAPP_TEMPLATE_LANGUAGE="en_US",
    )
    assert settings.connection_string == "canonical"
    assert settings.whatsapp_channel_id == "channel"
    assert settings.whatsapp_from == "+15550000001"
    assert settings.whatsapp_template_name == "template"
    assert settings.whatsapp_template_language == "en_GB"


def test_configuration_aliases_fall_back_to_legacy_values():
    from app.config import Settings

    settings = Settings(
        COMMUNICATION_SERVICES_CONNECTION_STRING="",
        AZURE_COMMUNICATION_CONNECTION_STRING="legacy",
        WHATSAPP_CHANNEL_ID="",
        AZURE_WHATSAPP_CHANNEL_ID="legacy-channel",
        WHATSAPP_FROM="",
        AZURE_WHATSAPP_FROM="+15550000002",
        WHATSAPP_TEMPLATE_NAME="",
        AZURE_WHATSAPP_TEMPLATE_NAME="legacy-template",
        WHATSAPP_TEMPLATE_LANGUAGE="",
        AZURE_WHATSAPP_TEMPLATE_LANGUAGE="en_US",
    )
    assert settings.connection_string == "legacy"
    assert settings.whatsapp_channel_id == "legacy-channel"
    assert settings.whatsapp_from == "+15550000002"
    assert settings.whatsapp_template_name == "legacy-template"
    assert settings.whatsapp_template_language == "en_US"


def test_auth_dependency_covers_enabled_and_disabled_modes(monkeypatch):
    from app.auth import require_auth, user_id_from_request, create_access_token
    from app.config import get_settings

    class Request:
        headers = {"X-API-Key": "secret-key"}

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    assert require_auth("") == "anonymous"
    assert user_id_from_request(Request()).startswith("usr_")

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-0123456789abcdef")
    get_settings.cache_clear()
    token = create_access_token("client-1", "usr_jwt_1")
    assert require_auth(f"Bearer {token}") == "usr_jwt_1"
    with pytest.raises(HTTPException) as missing:
        require_auth("")
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as wrong:
        require_auth("Bearer not.a.jwt")
    assert wrong.value.status_code == 401

    monkeypatch.setenv("JWT_SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as misconfigured:
        require_auth("Bearer x")
    assert misconfigured.value.status_code == 500
    get_settings.cache_clear()


def test_lifecycle_and_local_readiness(client):
    from app.main import health, readiness

    assert health()["auth_enabled"] is False
    response = readiness()
    assert response.status_code == 200
