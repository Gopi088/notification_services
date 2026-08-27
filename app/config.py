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

    # --- Logging ---
    # DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOG_LEVEL: str = "INFO"
    # "json" for production (Docker/AWS), "plain" for local development
    LOG_FORMAT: str = "plain"

    # --- Authentication ---
    # Set AUTH_ENABLED=true to require an API key on every request.
    # Clients must send the header:  X-API-Key: <AUTH_API_KEY>
    AUTH_ENABLED: bool = False
    AUTH_API_KEY: str = ""

    # --- OAuth2 / JWT ---
    JWT_SECRET: str = "super-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 60

    # --- Authorization ---
    # When AUTH_ENABLED=true, scopes are checked per API key.
    # Scopes are stored in the api_keys table as a JSON array.

    # --- Rate Limiting ---
    RATE_LIMIT_DEFAULT_PER_SECOND: int = 10
    RATE_LIMIT_BURST: int = 20

    # --- Idempotency ---
    IDEMPOTENCY_TTL_HOURS: int = 24

    # --- Worker Resilience ---
    WORKER_STALE_TIMEOUT_MINUTES: int = 5

    # --- Delivery SLA ---
    # How long (in seconds) a message may sit in "queued"/"sent" before the
    # status endpoint flags it as timed_out. Real delivery receipts arrive via
    # the webhook; this threshold tells callers when to stop waiting.
    DELIVERY_TIMEOUT_SECONDS: int = 300

    # --- Retry ---
    RETRY_MAX_ATTEMPTS: int = 3
    RETRY_BACKOFF_BASE_SECONDS: float = 0.5
    RETRY_BACKOFF_MAX_SECONDS: float = 30.0

    # --- Workers (thread pool) ---
    WORKER_COUNT: int = 3

    # --- Vonage SMS (alternative SMS provider to Azure) ---
    # If VONAGE_API_KEY and VONAGE_API_SECRET are set, the SMS channel uses
    # Vonage instead of Azure. Credentials from the Vonage dashboard.
    VONAGE_API_KEY: str = ""
    VONAGE_API_SECRET: str = ""
    # SMS sender ID (phone number in E.164 or an approved alphanumeric sender
    # ID like "Vonage APIs"). Must be a number you own / a sender registered
    # with Vonage.
    VONAGE_SMS_FROM: str = ""

    # --- Vonage WhatsApp Sandbox ---
    # When VONAGE_WHATSAPP_FROM is set, the WhatsApp channel uses the Vonage
    # Messages Sandbox instead of Azure. Credentials are shared with Vonage SMS.
    VONAGE_WHATSAPP_FROM: str = ""
    VONAGE_WHATSAPP_SANDBOX_URL: str = "https://messages-sandbox.nexmo.com/v1/messages"

    # --- Email templates ---
    # Directory holding channel templates. Email templates are HTML files that
    # can contain {{subject}} and {{body}} placeholders.
    TEMPLATES_DIR: str = "templates"
    # Default email template file (inside TEMPLATES_DIR/email/) used when a
    # request does not specify template_name.
    EMAIL_TEMPLATE_NAME: str = "default"

    # --- Azure Communication Services (WhatsApp + SMS + Email) ---
    # One connection string covers all three channels.
    # Canonical name: COMMUNICATION_SERVICES_CONNECTION_STRING.
    # Legacy alias AZURE_COMMUNICATION_CONNECTION_STRING is still honored so
    # existing .env files keep working (see `connection_string` property).
    # Azure portal -> your Communication Services resource -> Keys -> Connection string.
    COMMUNICATION_SERVICES_CONNECTION_STRING: str = ""
    AZURE_COMMUNICATION_CONNECTION_STRING: str = ""
    # Country code used when a phone number has no country code (91 = India).
    AZURE_DEFAULT_COUNTRY_CODE: str = "91"
    # SMS-enabled phone number from ACS (E.164, e.g. +919812345678).
    AZURE_SMS_FROM: str = ""
    # Verified sender address in Azure Email Communication Service,
    # e.g. DoNotReply@yourdomain.com
    AZURE_EMAIL_FROM: str = ""
    # WhatsApp channel registration ID (Azure portal -> Advanced Messaging).
    # Canonical name WHATSAPP_CHANNEL_ID; legacy AZURE_WHATSAPP_CHANNEL_ID is
    # also honored (see `whatsapp_channel_id` property).
    WHATSAPP_CHANNEL_ID: str = ""
    AZURE_WHATSAPP_CHANNEL_ID: str = ""
    # WhatsApp business number linked to the channel (E.164, informational -
    # used only for logging; the channel ID is what identifies the sender).
    # Canonical name WHATSAPP_FROM; legacy AZURE_WHATSAPP_FROM is honored.
    WHATSAPP_FROM: str = ""
    AZURE_WHATSAPP_FROM: str = ""
    # Approved WhatsApp template used for outbound messages to new contacts.
    # WhatsApp only allows free-form text inside a 24h session window (after the
    # recipient messages you first); every first message to a new number must be
    # a Meta-approved template. Leave empty to send free-form text (may not be
    # delivered to numbers that have never messaged you).
    # Canonical name WHATSAPP_TEMPLATE_NAME; legacy AZURE_WHATSAPP_TEMPLATE_NAME
    # is honored.
    WHATSAPP_TEMPLATE_NAME: str = ""
    AZURE_WHATSAPP_TEMPLATE_NAME: str = ""
    # Language of the approved template, e.g. "en_US" or "en".
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en_US"
    AZURE_WHATSAPP_TEMPLATE_LANGUAGE: str = "en_US"

    @property
    def connection_string(self) -> str:
        """One canonical connection string, honoring the legacy alias."""
        return (
            self.COMMUNICATION_SERVICES_CONNECTION_STRING
            or self.AZURE_COMMUNICATION_CONNECTION_STRING
        )

    @property
    def whatsapp_channel_id(self) -> str:
        """Canonical WhatsApp channel registration ID, honoring the alias."""
        return self.WHATSAPP_CHANNEL_ID or self.AZURE_WHATSAPP_CHANNEL_ID

    @property
    def whatsapp_from(self) -> str:
        """Canonical WhatsApp sender (for logging only)."""
        return self.WHATSAPP_FROM or self.AZURE_WHATSAPP_FROM

    @property
    def whatsapp_template_name(self) -> str:
        """Canonical default WhatsApp template name, honoring the alias."""
        return self.WHATSAPP_TEMPLATE_NAME or self.AZURE_WHATSAPP_TEMPLATE_NAME

    @property
    def whatsapp_template_language(self) -> str:
        """Canonical default WhatsApp template language, honoring the alias."""
        return self.WHATSAPP_TEMPLATE_LANGUAGE or self.AZURE_WHATSAPP_TEMPLATE_LANGUAGE


@lru_cache
def get_settings() -> Settings:
    return Settings()
