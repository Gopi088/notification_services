"""
Application configuration.

All values are loaded from environment variables / a `.env` file in the
project root. See `.env.example` for the full list of supported keys.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    APP_NAME: str = "Notification Service"
    # When MOCK_MODE=true, no real provider APIs are called. Messages are
    # "sent" locally and marked delivered/failed pseudo-randomly. This lets
    # you exercise the full API from the CLI without any real credentials.
    MOCK_MODE: bool = True
    DATABASE_PATH: str = "notifications.db"

    # --- Azure Communication Services (WhatsApp + SMS + Email) ---
    # One connection string covers all three channels.
    # Azure portal -> your Communication Services resource -> Keys.
    AZURE_COMMUNICATION_CONNECTION_STRING: str = ""
    # Country code used when a phone number has no country code (91 = India).
    AZURE_DEFAULT_COUNTRY_CODE: str = "91"
    # SMS-enabled phone number from ACS (E.164, e.g. +919812345678).
    AZURE_SMS_FROM: str = ""
    # Verified sender address in Azure Email Communication Service,
    # e.g. DoNotReply@yourdomain.com
    AZURE_EMAIL_FROM: str = ""
    # WhatsApp channel registration ID (Azure portal -> Advanced Messaging).
    AZURE_WHATSAPP_CHANNEL_ID: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
