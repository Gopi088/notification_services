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

    # --- Twilio (used for WhatsApp + SMS) ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = ""   # e.g. whatsapp:+14155238886
    TWILIO_SMS_FROM: str = ""        # e.g. +14155551234

    # --- SMTP (used for Email) ---
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
