"""
Application configuration.

All values are loaded from environment variables / a `.env` file in the
project root. See `.env.example` for the full list of supported keys.
"""
from functools import lru_cache
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    APP_NAME: str = "Notification Service"
    # Server bind address/port (used by run.sh and Docker). HOST/PORT are read
    # from the environment so the service can be launched on any address
    # without rebuilding. Defaults: 127.0.0.1:8000.
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    # When MOCK_MODE=true, no real provider APIs are called. Messages are
    # "sent" locally and marked delivered/failed pseudo-randomly. This lets
    # you exercise the full API from the CLI without any real credentials.
    MOCK_MODE: bool = True
    DATABASE_PATH: str = "notifications.db"

    # --- Authentication ---
    # Set AUTH_ENABLED=true to require an API key on notification API routes.
    AUTH_ENABLED: bool = False
    AUTH_API_KEY: str = ""

    # --- JWT authentication (replaces X-API-Key for /api/v1/*) ---
    # Signing secret used to issue/verify access tokens. Generate one, e.g.:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    # NEVER hardcode or commit a real secret.
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Login credentials: the client authenticates with client_id + client_secret
    # at /api/v1/auth/login and receives a JWT. Defaults to AUTH_CLIENT_SECRET
    # falling back to AUTH_API_KEY when unset.
    AUTH_CLIENT_ID: str = "notification-service"
    AUTH_CLIENT_SECRET: str = ""

    # --- Delivery SLA ---
    # How long (in seconds) a message may sit in "queued"/"sent" before the
    # status endpoint flags it as timed_out. Real delivery receipts arrive via
    # the webhook; this threshold tells callers when to stop waiting.
    DELIVERY_TIMEOUT_SECONDS: int = 300

    # --- Payload limits (production safety, configurable via .env) ---
    # Maximum message length per channel. Enforced BEFORE processing/sending;
    # exceeding the channel limit returns HTTP 413 with a clear error.
    SMS_MAX_MESSAGE_LENGTH: int = 1600
    WHATSAPP_MAX_MESSAGE_LENGTH: int = 4096
    EMAIL_MAX_MESSAGE_LENGTH: int = 100000
    # Maximum size of a single email attachment file (decoded bytes).
    MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    # Maximum number of pages in an attached document (email).
    MAX_DOCUMENT_PAGES: int = 100
    # Maximum total request payload size (serialized JSON bytes).
    MAX_REQUEST_SIZE_BYTES: int = 12 * 1024 * 1024  # 12 MB

    # --- Duplicate detection ---
    # Window (minutes) during which sending the SAME content (user + channel +
    # recipient + message/template) to the same recipient is treated as a
    # duplicate and blocked with a "do you want to resend?" response. Sends of
    # the same message OUTSIDE this window are treated as new notifications.
    # Set to 0 to disable window-based duplicate detection.
    DUPLICATE_WINDOW_MINUTES: int = 30

    # --- Storage / Database ---
    # Storage backend: "sqlite" (dev/fallback) or "postgres" (production).
    # PostgreSQL is the durable source of truth in the target architecture.
    STORAGE_BACKEND: str = "sqlite"
    # SQLite file path (used when STORAGE_BACKEND=sqlite).
    DATABASE_PATH: str = "notifications.db"
    # PostgreSQL DSN (used when STORAGE_BACKEND=postgres).
    # Example: postgresql://user:pass@localhost:5432/notifications
    DATABASE_URL: str = ""
    DB_POOL_MIN: int = 5
    DB_POOL_MAX: int = 50

    # --- Queue / Redis Streams ---
    # If true, the API enqueues notifications to a queue and workers deliver
    # them asynchronously. If false, delivery happens in-process via
    # BackgroundTasks (backward-compatible fallback for dev).
    QUEUE_ENABLED: bool = False
    # Queue backend: "redis" (Redis Streams + workers, production) or
    # "memory" (in-process asyncio queue, local single-instance dev).
    QUEUE_BACKEND: str = "redis"
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: str = ""
    # Stream namespaces (per channel). Retry/DLQ streams are derived from these.
    QUEUE_STREAM_PREFIX: str = "notifications"
    QUEUE_MESSAGE_MAX_BYTES: int = 65536
    # Consumer group name shared by all workers of the same stream.
    QUEUE_CONSUMER_GROUP: str = "workers"
    # Visibility timeout for XAUTOCLAIM (ms) - how long before a pending
    # message is reclaimed after a worker dies.
    QUEUE_VISIBILITY_TIMEOUT_MS: int = 30000
    # Block time (ms) for XREADGROUP.
    QUEUE_BLOCK_MS: int = 5000

    # --- Worker ---
    WORKER_CONCURRENCY: int = 4
    WORKER_GRACE_SECONDS: int = 30
    WORKER_CONCURRENCY_WHATSAPP: int = 2
    WORKER_CONCURRENCY_SMS: int = 4
    WORKER_CONCURRENCY_EMAIL: int = 4

    # --- Retry ---
    MAX_ATTEMPTS: int = 5
    RETRY_BASE_DELAY_MS: int = 5000
    RETRY_MAX_DELAY_MS: int = 120000
    RETRY_JITTER_RATIO: float = 0.2

    # --- Idempotency ---
    # Client Idempotency-Key header TTL (seconds). Must cover the retry horizon.
    IDEMPOTENCY_TTL_SECONDS: int = 86400

    # --- Rate limiting ---
    RATELIMIT_ENABLED: bool = False
    RATE_LIMIT_PER_KEY: int = 100
    RATE_LIMIT_PER_KEY_WINDOW_SECONDS: int = 60
    RATE_LIMIT_PER_RECIPIENT: int = 20
    RATE_LIMIT_PER_RECIPIENT_WINDOW_SECONDS: int = 3600
    RATE_LIMIT_PER_CHANNEL: int = 500
    RATE_LIMIT_PER_CHANNEL_WINDOW_SECONDS: int = 60

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"  # "text" (dev) or "json" (structured)
    LOG_REDACT_KEYS: str = ""  # extra comma-separated field names to redact
    # When true, lightweight in-process performance metrics are collected
    # (request handler, SQLite, Redis, dispatch, audit timings) and exposed via
    # GET /api/v1/performance/metrics plus a periodic summary log. Metrics are
    # PER-PROCESS (each Uvicorn worker collects its own). Default off so the
    # hot path is unchanged unless explicitly enabled.
    PERFORMANCE_METRICS_ENABLED: bool = False
    # Seconds between periodic aggregated-metrics log lines (metrics only).
    PERFORMANCE_METRICS_LOG_INTERVAL_SECONDS: int = 60
    # When true (default) and LOG_FORMAT=text, terminal logs are ANSI-coloured
    # by level (DEBUG blue, INFO green, WARNING yellow, ERROR red, CRITICAL
    # magenta). File logs are never coloured.
    LOG_COLORS: bool = True
    # Optional independent file level (standard threshold semantics). When
    # empty, the file handler uses LOG_LEVEL (but unlike the terminal, it does
    # not apply the exact-level filter, so WARNING/ERROR are still written to
    # the log file).
    LOG_FILE_LEVEL: str = ""
    # Application log file (optional). When set, logs are also written to a
    # rotating file (in addition to stdout/stderr). Empty = stdout only.
    LOG_FILE: str = ""
    LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
    LOG_FILE_BACKUPS: int = 5
    # Audit log file (JSON lines) - separate from application logs. When set,
    # audit records are also appended here (durable even if DB is unavailable).
    AUDIT_LOG_FILE: str = ""
    # Inbound (reply) handling.
    # When true, an auto-reply is sent back to recipients who reply to a
    # notification (2-way conversations). Set INBOUND_AUTO_REPLY_TEXT to the
    # reply body.
    INBOUND_AUTO_REPLY: bool = False
    INBOUND_AUTO_REPLY_TEXT: str = ""

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

    # --- Twilio (optional SMS + WhatsApp provider) ---
    # When TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are set, the SMS and
    # WhatsApp channels use Twilio instead of Vonage/Azure. Credentials from
    # the Twilio console -> Account -> API keys & tokens. NEVER commit real
    # secrets.
    # Twilio Messages API base URL. Override for local dummy-provider testing
    # (default: https://api.twilio.com). Never point production at a dummy.
    TWILIO_API_BASE_URL: str = "https://api.twilio.com"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    # Sender number for the SMS channel (E.164, e.g. +17372508034).
    TWILIO_FROM: str = ""
    # Predefined SMS template name (Twilio TRIAL accounts can ONLY send
    # predefined SMS templates, not free-form text - error 572006 otherwise).
    # When set, free-form SMS is automatically retried as this template when
    # Twilio rejects the free-form body. Example: sms_appointment_reminders.
    TWILIO_SMS_TEMPLATE: str = ""
    # WhatsApp sender number (E.164). Falls back to TWILIO_FROM when empty.
    TWILIO_WHATSAPP_FROM: str = ""
    # Approved WhatsApp content template SID (e.g. HXfe5ab5f00277942d4d4200328b4d403c).
    # Used for template sends to new contacts (no 24h session required).
    TWILIO_WHATSAPP_CONTENT_SID: str = ""
    # Optional mapping "friendly template name -> content SID" used when a
    # request passes a template_name, e.g.
    #   test_template=HXfe5ab5f00277942d4d4200328b4d403c;reminder=HX...
    # (JSON object form is also accepted: {"test_template": "HX..."}).
    TWILIO_WHATSAPP_TEMPLATES: str = ""
    # Public URL where Twilio posts delivery-status callbacks for SMS and
    # WhatsApp messages. When set, the provider includes `StatusCallback` so
    # the app can transition submitted -> delivered/failed automatically.
    # Example: https://your-host/api/v1/twilio/status
    TWILIO_STATUS_CALLBACK_URL: str = ""

    # --- Delivery-status webhook URLs (per channel) ---
    # Public URLs where the providers post delivery callbacks. These override
    # TWILIO_STATUS_CALLBACK_URL for the respective channel. Point them at
    # your public host (e.g. an ngrok URL) during local development.
    #   https://<ngrok-domain>/api/v1/sms/webhook
    #   https://<ngrok-domain>/api/v1/whatsapp/webhook
    SMS_STATUS_WEBHOOK_URL: str = ""
    WHATSAPP_STATUS_WEBHOOK_URL: str = ""

    # --- Twilio dedicated status-callback URLs (preferred) ---
    # Twilio posts outbound delivery status here (form-encoded). These take
    # precedence over SMS_STATUS_WEBHOOK_URL / WHATSAPP_STATUS_WEBHOOK_URL.
    #   https://<host>/api/v1/twilio/sms/status
    #   https://<host>/api/v1/twilio/whatsapp/status
    TWILIO_SMS_STATUS_CALLBACK_URL: str = ""
    TWILIO_WHATSAPP_STATUS_CALLBACK_URL: str = ""

    # --- Delivery polling / reconciliation (fallback to webhooks) ---
    # When true, on-demand status polling (e.g. GET status -> query provider)
    # is allowed as a fallback when no webhook is configured or a callback
    # never arrives. DELIVERY_RECONCILIATION_ENABLED additionally enables a
    # periodic reconciliation sweep of stuck submitted/sent messages.
    DELIVERY_POLLING_ENABLED: bool = True
    DELIVERY_POLLING_INTERVAL_SECONDS: int = 300
    DELIVERY_RECONCILIATION_ENABLED: bool = False

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

    @property
    def twilio_whatsapp_from(self) -> str:
        """WhatsApp sender number, falling back to the shared Twilio sender."""
        return self.TWILIO_WHATSAPP_FROM or self.TWILIO_FROM

    @property
    def auth_client_secret_effective(self) -> str:
        """Login credential: AUTH_CLIENT_SECRET, else AUTH_API_KEY."""
        return self.AUTH_CLIENT_SECRET or self.AUTH_API_KEY

    @property
    def twilio_whatsapp_templates(self) -> dict:
        """Parse TWILIO_WHATSAPP_TEMPLATES ("name=HX...;name2=HX..." or JSON)."""
        raw = (self.TWILIO_WHATSAPP_TEMPLATES or "").strip()
        if not raw:
            return {}
        if raw.lstrip().startswith("{"):
            try:
                import json as _json

                return {str(k): str(v) for k, v in _json.loads(raw).items()}
            except (ValueError, TypeError):
                return {}
        mapping: dict = {}
        for part in re.split(r"[;,]", raw):
            if "=" in part:
                k, _, v = part.partition("=")
                mapping[k.strip()] = v.strip()
        return mapping


@lru_cache
def get_settings() -> Settings:
    return Settings()
