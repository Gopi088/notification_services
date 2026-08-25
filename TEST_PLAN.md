# TEST_PLAN.md

**Project:** Notification Service (`notification-service`)
**Version:** 2.0.0
**Goal:** Minimum **90% automated test coverage**
**Document purpose:** Specification from which test cases will be implemented.

---

## 1. System Architecture

The project is a **Python 3.12 / FastAPI** notification service that sends messages
over three channels (WhatsApp, SMS, Email), each backed by an external provider,
with a local SQLite database for delivery tracking.

```
Client (curl / CLI / REST caller)
        │  POST /api/v1/notifications/send | /api/v1/notifications/event | /send
        ▼
Notification API (FastAPI)
  ├── app/main.py                  → app bootstrap, routers, exception handler
  ├── app/routers/v1.py            → /api/v1/* routes (recommended)
  ├── app/routers/notifications.py → legacy /send, /status/{id} (v0)
  └── app/routers/webhooks.py      → /api/v1/whatsapp/webhook (delivery receipts)
        │
        ▼
Authentication (app/auth.py)
  → require_api_key (X-API-Key header, only when AUTH_ENABLED=true)
        │
        ▼
Validation (app/validation.py)
  → validate_contact (phone E.164-ish / email regex per channel)
  → Pydantic schemas (app/schemas.py)
        │
        ▼
Orchestrator (app/orchestrator.py)
  → orchestrate_send / orchestrate_event (one queued record per channel, one group_id)
  → background delivery via FastAPI BackgroundTasks
  → status summaries, elapsed-time / timeout computation
        │
        ▼
Provider Factory (app/providers/factory.py)
  └── get_provider(channel)
      ├── sms      → VonageSMSProvider (preferred) | AzureSMSProvider (fallback)
      ├── whatsapp → VonageWhatsAppProvider (preferred) | AzureWhatsAppProvider (fallback)
      └── email    → AzureEmailProvider
        │
        ▼
Channel Providers (app/providers/)
  ├── base.py             → NotificationProvider ABC, ProviderResult, ProviderError
  ├── azure_provider.py   → AzureSMSProvider, AzureEmailProvider, AzureWhatsAppProvider,
  │                         _normalize_phone, attachment download/SSRF validation
  └── vonage_provider.py  → VonageSMSProvider (Vonage Messages SDK),
                            VonageWhatsAppProvider (HTTP POST to sandbox)
        │
        ▼
External APIs
  ├── Vonage Messages API (SMS)          https://api.nexmo.com/v1/messages
  ├── Vonage WhatsApp Sandbox            https://messages-sandbox.nexmo.com/v1/messages
  └── Azure Communication Services       (SMS / Email / WhatsApp via connection string)
        │
        ▼
Persistence (app/database.py)
  → SQLite "messages" table, group_id grouping, status transitions
```

### Key modules and responsibilities

| Module | Responsibility |
| ------ | -------------- |
| `app/main.py` | FastAPI app, startup `init_db()`, global exception handler → 500 |
| `app/config.py` | `Settings` (pydantic-settings), canonical + legacy env names, `get_settings()` |
| `app/auth.py` | `require_api_key` dependency (HMAC-safe compare, 401/500) |
| `app/schemas.py` | Pydantic request/response models, `Channel`/`Status` enums |
| `app/validation.py` | `validate_contact`, `ContactValidationError` |
| `app/orchestrator.py` | Send/event fan-out, background dispatch, status aggregation |
| `app/database.py` | SQLite CRUD + status transitions + `update_status_by_provider_id` |
| `app/templates.py` | Email HTML template rendering, path-traversal protection |
| `app/providers/base.py` | Provider interface + shared exceptions |
| `app/providers/factory.py` | Channel → provider selection (env-driven) |
| `app/providers/azure_provider.py` | Azure SMS/Email/WhatsApp, attachment SSRF checks |
| `app/providers/vonage_provider.py` | Vonage SMS SDK + Vonage WhatsApp sandbox HTTP |
| `app/routers/v1.py` | `POST /api/v1/notifications/send`, `/event`, `GET .../status` |
| `app/routers/notifications.py` | Legacy `POST /send`, `GET /status/{id}` |
| `app/routers/webhooks.py` | Event Grid validation + WhatsApp delivery receipts |
| `notification_service.py` | Standalone CLI (server auto-start, send/status/event commands) |

### Entry points

- **API server:** `uvicorn app.main:app --host 127.0.0.1 --port 8000`
- **CLI:** `python3 notification_service.py` (interactive) or `send/status/send-event` subcommands

---

## 2. Notification Channels

### 2.1 SMS

| Attribute | Value |
| --------- | ----- |
| Channel name | `sms` |
| Primary provider | `VonageSMSProvider` (when `VONAGE_API_KEY` + `VONAGE_API_SECRET` set) |
| Fallback provider | `AzureSMSProvider` |
| Module | `app/providers/vonage_provider.py`, `app/providers/azure_provider.py` |
| Request format | `{"channels":[{"channel":"sms","contact":"9887270348"}],"message":"..."}` |
| Authentication | Vonage: Basic auth (`VONAGE_API_KEY`:`VONAGE_API_SECRET`); Azure: connection string |
| External API | Vonage `POST https://api.nexmo.com/v1/messages` (Sdk `vonage_messages.Sms`) |
| Success response | 202 `{"status":"queued", ...}`; tracked status `sent` |
| Failure response | tracked status `failed` + error message |
| Provider message ID | `message_uuid` (Vonage) / `result.message_id` (Azure) |
| Status handling | `queued → sent → delivered/failed` (delivered via webhook or mock) |

Testing strategy: mock Vonage SDK `client.messages.send`; never send real SMS.

### 2.2 WhatsApp

| Attribute | Value |
| --------- | ----- |
| Channel name | `whatsapp` |
| Primary provider | `VonageWhatsAppProvider` (when `VONAGE_WHATSAPP_FROM` set) |
| Fallback provider | `AzureWhatsAppProvider` |
| Module | `app/providers/vonage_provider.py`, `app/providers/azure_provider.py` |
| Request format | `{"channels":[{"channel":"whatsapp","contact":"919887270348"}],"message":"..."}` |
| Authentication | Vonage: Basic auth; Azure: connection string |
| External API | Vonage `POST https://messages-sandbox.nexmo.com/v1/messages` (`requests.post`); Azure `NotificationMessagesClient` |
| Success response | 202 `{"status":"queued", ...}`; tracked status `sent` |
| Failure response | tracked status `failed` + error message |
| Provider message ID | Vonage `message_uuid`; Azure `receipt.message_id` |
| Status handling | `queued → sent → delivered/failed/read` (via `/api/v1/whatsapp/webhook`) |

Testing strategy: mock `requests.post` (Vonage) and Azure SDK clients; never send real WhatsApp.

### 2.3 Email

| Attribute | Value |
| --------- | ----- |
| Channel name | `email` |
| Provider | `AzureEmailProvider` |
| Module | `app/providers/azure_provider.py` |
| Request format | `{"channels":[{"channel":"email","contact":"a@b.com"}],"message":"..."}` |
| Authentication | Azure connection string |
| External API | Azure `EmailClient` (`begin_send`) |
| Success response | 202 `{"status":"queued", ...}`; tracked status `sent` |
| Failure response | tracked status `failed` + error message |
| Provider message ID | `result.get("message_id")` |
| Status handling | `queued → sent → delivered/failed` |
| Extra features | HTML body, subject, cc/bcc, replyTo, attachments (url/base64), template rendering |

Testing strategy: mock `EmailClient` and attachment HTTP fetches; never send real email.

---

## 3. Testing Strategy

### 3.1 Unit Tests

Target individual functions/classes with external APIs **mocked**:

- `app/validation.py` — `validate_contact` for all channels.
- `app/providers/azure_provider.py` — `_normalize_phone`, `_build_attachments`,
  `_validate_url` (SSRF checks), `_fetch_as_base64`.
- `app/providers/vonage_provider.py` — payload construction, response parsing.
- `app/templates.py` — `render_email`, `render_sms`, path traversal, missing template.
- `app/orchestrator.py` — `_delivery_message`, `_delivery_detail`, status aggregation.
- `app/database.py` — CRUD and status transitions (against a temp DB).
- `app/config.py` — settings loading, canonical/legacy aliases.
- `app/routers/webhooks.py` — `_redact`, `_extract_failure`, `_log_event_safe`.

### 3.2 Integration Tests

Test interactions between layers with providers mocked:

- API route → orchestrator → provider (mocked) → database.
- Webhook → database status update.
- Config → provider selection in factory.

### 3.3 API Tests

Use `fastapi.testclient.TestClient` to exercise every endpoint:

- Positive, negative, boundary, and error scenarios (see Section 4 matrix).

---

## 4. Test Case Matrix

Legend — Test Type: `U` unit, `I` integration, `A` API.
Priority: `H` high, `M` medium, `L` low.

| Test ID | Component | Scenario | Input | Expected Result | Test Type | Priority |
| ------- | --------- | -------- | ----- | --------------- | --------- | -------- |
| TC-001 | config | Valid settings load | `MOCK_MODE=false` + valid creds | settings fields populated | U | H |
| TC-002 | config | Missing connection string | `COMMUNICATION_SERVICES_CONNECTION_STRING=` | `connection_string == ""` | U | H |
| TC-003 | config | Legacy Azure alias honored | only `AZURE_COMMUNICATION_CONNECTION_STRING` set | `connection_string` returns it | U | H |
| TC-004 | config | WhatsApp channel ID aliases | `WHATSAPP_CHANNEL_ID` + legacy | `whatsapp_channel_id` canonical | U | M |
| TC-005 | validation | Valid phone (sms) | `9887270348` | passes | U | H |
| TC-006 | validation | Valid phone with `+` | `+919887270348` | passes | U | H |
| TC-007 | validation | Invalid phone | `123` | `ContactValidationError` | U | H |
| TC-008 | validation | Phone with letters | `98872x70348` | `ContactValidationError` | U | M |
| TC-009 | validation | Valid email | `a@b.com` | passes | U | H |
| TC-010 | validation | Invalid email | `not-an-email` | `ContactValidationError` | U | H |
| TC-011 | validation | Whitespace-only phone | `"   "` | `ContactValidationError` | U | M |
| TC-012 | auth | AUTH disabled | `AUTH_ENABLED=false` | request allowed | U/A | H |
| TC-013 | auth | AUTH enabled, valid key | correct `X-API-Key` | request allowed | A | H |
| TC-014 | auth | AUTH enabled, missing key | no header | 401 | A | H |
| TC-015 | auth | AUTH enabled, wrong key | wrong header | 401 | A | H |
| TC-016 | auth | AUTH enabled, key not configured | `AUTH_ENABLED=true`, `AUTH_API_KEY=` | 500 server_config_error | U/A | M |
| TC-017 | normalize_phone | 10-digit Indian | `9887270348` | `+919887270348` | U | H |
| TC-018 | normalize_phone | Leading zero | `0-98872-70348` | `+919887270348` | U | H |
| TC-019 | normalize_phone | 12-digit with country code | `919887270348` | `+919887270348` | U | H |
| TC-020 | normalize_phone | Already E.164 | `+919887270348` | unchanged | U | H |
| TC-021 | normalize_phone | 11-digit non-zero | `98872703481` | `+98872703481` (best effort) | U | L |
| TC-022 | templates | Render default email | body/subject | HTML with escaped values | U | H |
| TC-023 | templates | Render named template | `template_name="default"` | HTML rendered | U | H |
| TC-024 | templates | Missing named template | `template_name="nope"` | `TemplateError` | U | H |
| TC-025 | templates | Missing default template | no files | plain `<p>` fallback | U | M |
| TC-026 | templates | HTML escaping | body `<script>` | escaped output | U | M |
| TC-027 | templates | Path traversal blocked | `template_name="../../etc/passwd"` | safe name used / not found | U | M |
| TC-028 | database | Create message | valid params | row inserted | U | H |
| TC-029 | database | Update status | status + provider + id | row updated | U | H |
| TC-030 | database | Update by provider id | provider_message_id | row updated | U | H |
| TC-031 | database | Get message exists | existing id | row returned | U | H |
| TC-032 | database | Get message missing | unknown id | `None` | U | H |
| TC-033 | database | Get group | group_id | ordered rows | U | M |
| TC-034 | database | Duplicate message_id | insert twice | `IntegrityError` | U | M |
| TC-035 | database | List messages filter | `channel="sms"` | filtered rows | U | M |
| TC-036 | orchestrator | `_delivery_message` whatsapp template | payload with template.id | `"[template_id]"` | U | M |
| TC-037 | orchestrator | `_delivery_message` data string | data="text" | returns data | U | M |
| TC-038 | orchestrator | `_delivery_detail` timed out | old `sent` row | `timed_out=True` | U | H |
| TC-039 | orchestrator | `_delivery_detail` fresh | new row | `timed_out=False` | U | H |
| TC-040 | orchestrator | Group summary all delivered | rows delivered | overall `delivered` | U | M |
| TC-041 | orchestrator | Group summary mixed | delivered + failed | overall `partial` | U | M |
| TC-042 | orchestrator | `_safe_send` success | provider returns result | status `sent` + provider id | I | H |
| TC-043 | orchestrator | `_safe_send` ProviderError | provider raises | status `failed` + error | I | H |
| TC-044 | orchestrator | `_safe_send` unexpected | provider raises Exception | status `failed` "Unexpected error" | I | M |
| TC-045 | factory | SMS → Vonage configured | Vonage creds set | `VonageSMSProvider` | U | H |
| TC-046 | factory | SMS → Azure fallback | Vonage creds missing | `AzureSMSProvider` | U | H |
| TC-047 | factory | WhatsApp → Vonage configured | `VONAGE_WHATSAPP_FROM` set | `VonageWhatsAppProvider` | U | H |
| TC-048 | factory | WhatsApp → Azure fallback | `VONAGE_WHATSAPP_FROM` missing | `AzureWhatsAppProvider` | U | H |
| TC-049 | factory | Email | any config | `AzureEmailProvider` | U | H |
| TC-050 | provider-sms | Successful send | valid inputs | `ProviderResult` status sent | U | H |
| TC-051 | provider-sms | Correct payload | mock SDK capture | to/from/text correct | U | H |
| TC-052 | provider-sms | Auth error | SDK raises | `ProviderError` w/ message | U | H |
| TC-053 | provider-sms | Missing API key | `VONAGE_API_KEY=` | `ProviderConfigError` | U | H |
| TC-054 | provider-sms | Missing API secret | `VONAGE_API_SECRET=` | `ProviderConfigError` | U | H |
| TC-055 | provider-sms | Missing SMS from | `VONAGE_SMS_FROM=` | `ProviderConfigError` | U | H |
| TC-056 | provider-sms | Malformed response | no message_uuid | `ProviderError` | U | M |
| TC-057 | provider-sms | Timeout | SDK raises TimeoutError | `ProviderError` | U | M |
| TC-058 | provider-whatsapp | Successful send | mock `requests.post` 200 | `ProviderResult` + uuid | U | H |
| TC-059 | provider-whatsapp | Correct payload | capture body | from/to/message_type/text/channel | U | H |
| TC-060 | provider-whatsapp | Number normalized | `+919887270348` | `to == "919887270348"` | U | H |
| TC-061 | provider-whatsapp | Missing API key | `VONAGE_API_KEY=` | `ProviderConfigError` | U | H |
| TC-062 | provider-whatsapp | Missing API secret | `VONAGE_API_SECRET=` | `ProviderConfigError` | U | H |
| TC-063 | provider-whatsapp | Missing WhatsApp from | `VONAGE_WHATSAPP_FROM=` | `ProviderConfigError` | U | H |
| TC-064 | provider-whatsapp | HTTP 401 | mock 401 | `ProviderError` "authentication" | U | H |
| TC-065 | provider-whatsapp | HTTP 403 sandbox | mock 403 | `ProviderError` "allow-listed" | U | H |
| TC-066 | provider-whatsapp | HTTP 500 | mock 500 | `ProviderError` with code | U | M |
| TC-067 | provider-whatsapp | Network error | mock ConnectionError | `ProviderError` "network" | U | M |
| TC-068 | provider-whatsapp | No message_uuid | 200 without uuid | `ProviderError` | U | M |
| TC-069 | provider-whatsapp | Secret not in error | 500 body | secret absent from error | U | H |
| TC-070 | provider-azure-sms | Success | mock SmsClient | `ProviderResult` sent | U | H |
| TC-071 | provider-azure-sms | Provider reports failure | result.successful=False | `ProviderError` | U | H |
| TC-072 | provider-azure-sms | Missing SMS from | `AZURE_SMS_FROM=` | `ProviderConfigError` | U | H |
| TC-073 | provider-azure-sms | Missing connection string | empty conn | `ProviderConfigError` | U | H |
| TC-074 | provider-azure-email | Success | mock EmailClient | `ProviderResult` sent | U | H |
| TC-075 | provider-azure-email | Missing from | `AZURE_EMAIL_FROM=` | `ProviderConfigError` | U | H |
| TC-076 | provider-azure-email | Template send | `send_with_template` | renders + sends | U | M |
| TC-077 | provider-azure-email | Attachment via base64 | content_base64 | included in message | U | M |
| TC-078 | provider-azure-email | Attachment via url | mocked fetch | base64 included | U | M |
| TC-079 | provider-azure-email | Attachment http url | `http://...` | `ProviderError` (https only) | U | H |
| TC-080 | provider-azure-email | Attachment localhost | `https://localhost/x` | `ProviderError` (SSRF) | U | H |
| TC-081 | provider-azure-email | Attachment private IP | `https://192.168.1.1/x` | `ProviderError` (SSRF) | U | H |
| TC-082 | provider-azure-email | Attachment redirect | 302 response | `ProviderError` redirect | U | M |
| TC-083 | provider-azure-email | Attachment oversized | > 20 MB | `ProviderError` | U | M |
| TC-084 | provider-azure-email | No message id in result | result without id | `ProviderResult` with empty id | U | L |
| TC-085 | provider-azure-whatsapp | Text send success | mock client | `ProviderResult` sent | U | H |
| TC-086 | provider-azure-whatsapp | Template send success | mock client | sent + template payload | U | H |
| TC-087 | provider-azure-whatsapp | Missing channel id | `WHATSAPP_CHANNEL_ID=` | `ProviderConfigError` | U | H |
| TC-088 | provider-azure-whatsapp | No template name | empty | `ProviderConfigError` | U | H |
| TC-089 | provider-azure-whatsapp | Receipt has error | receipt.error set | `ProviderError` | U | M |
| TC-090 | provider-azure-whatsapp | No receipts | empty receipts | `ProviderError` | U | M |
| TC-091 | webhooks | `_redact` masks secrets | dict with token | value `***` | U | H |
| TC-092 | webhooks | `_extract_failure` error dict | error.code+message | tuple extracted | U | H |
| TC-093 | webhooks | `_extract_failure` details array | empty message + details | detail message used | U | H |
| TC-094 | webhooks | `_extract_failure` errorCode level | errorCode/errorMessage | extracted | U | H |
| TC-095 | webhooks | `_extract_failure` none | no error fields | `(None, None)` | U | M |
| TC-096 | webhooks | Validation event | validationCode | 200 `{"validationResponse": code}` | A | H |
| TC-097 | webhooks | Delivered event | messageId + delivered | DB status `delivered` | I | H |
| TC-098 | webhooks | Failed event | messageId + failed + error | DB status `failed` + reason | I | H |
| TC-099 | webhooks | Read event | read status | DB status `delivered` | I | M |
| TC-100 | webhooks | Non-whatsapp event | channelType other | ignored | I | M |
| TC-101 | webhooks | Missing validationCode | POST validation no code | continue (no response field) | I | M |
| TC-102 | API send | Valid whatsapp | single channel | 202 + queued | A | H |
| TC-103 | API send | Valid sms | single channel | 202 + queued | A | H |
| TC-104 | API send | Valid email | single channel | 202 + queued | A | H |
| TC-105 | API send | Multi-channel | whatsapp+sms+email | 202 + 3 queued | A | H |
| TC-106 | API send | Missing channel | no channels key | 422 | A | H |
| TC-107 | API send | Empty channels | `[]` | 422 | A | H |
| TC-108 | API send | Duplicate channel | whatsapp twice | 422 | A | H |
| TC-109 | API send | Invalid channel | `"fax"` | 422 | A | H |
| TC-110 | API send | Missing message | no message | 422 | A | H |
| TC-111 | API send | Empty message | `""` | 422 | A | H |
| TC-112 | API send | Whitespace message | `"   "` | 422 | A | H |
| TC-113 | API send | Message too long | 4097 chars | 422 | A | M |
| TC-114 | API send | Message boundary 4096 | 4096 chars | 202 | A | L |
| TC-115 | API send | Missing contact | no contact | 422 | A | H |
| TC-116 | API send | Invalid phone | `"abc"` | 400 validation_error | A | H |
| TC-117 | API send | Invalid email | `"nope"` | 400 validation_error | A | H |
| TC-118 | API send | Template name valid | whatsapp + template_name | 202 | A | H |
| TC-119 | API send | Template params | template_params list | 202 + sent | A | M |
| TC-120 | API send | Reference included | `reference` field | echoed in response | A | M |
| TC-121 | API send | Malformed JSON | invalid body | 422 | A | M |
| TC-122 | API event | Valid event | deliveries list | 202 | A | H |
| TC-123 | API event | WhatsApp payload template | template.id | 202 | A | M |
| TC-124 | API event | SMS payload | message | 202 | A | M |
| TC-125 | API event | Email payload html/cc | rich email | 202 | A | M |
| TC-126 | API event | Empty deliveries | `[]` | 422 | A | H |
| TC-127 | API event | Invalid payload type | wrong shape | 422 | A | M |
| TC-128 | API status | Group exists | valid group id | 200 + status | A | H |
| TC-129 | API status | Single message id | valid message id | 200 + status | A | H |
| TC-130 | API status | Not found | random uuid | 404 | A | H |
| TC-131 | API status | Delivered shown | message delivered | status `delivered` | A | M |
| TC-132 | API status | Failed with error | failed message | error text present | A | M |
| TC-133 | API status | Timed out flag | old sent message | `timed_out=true` | A | M |
| TC-134 | API health | Success | GET /health | 200 | A | L |
| TC-135 | API legacy send | Valid | legacy payload | 202 + message_id | A | M |
| TC-136 | API legacy send | Invalid channel | bad channel | 422 | A | M |
| TC-137 | API legacy status | Exists | valid id | 200 | A | M |
| TC-138 | API legacy status | Missing | unknown | 404 | A | M |
| TC-139 | lifecycle | Full success | send → provider → sent | DB status sent | I | H |
| TC-140 | lifecycle | Provider failure | provider raises | DB status failed | I | H |
| TC-141 | lifecycle | Partial failure | 2 channels, 1 fails | overall `partial` | I | H |
| TC-142 | lifecycle | MOCK delivered | MOCK_MODE | status delivered (async) | I | M |
| TC-143 | security | Secret not in API response | failed send error | no secret string | A | H |
| TC-144 | security | Secret not in webhook log | event with token | redacted `***` | U | H |
| TC-145 | security | Injection-style contact | `"'; DROP TABLE"` | validation error / no SQLi | A | M |
| TC-146 | security | Long unicode message | emoji + unicode | 202 | A | M |
| TC-147 | security | Extra fields ignored | unexpected JSON key | accepted (extra ignore) | A | L |
| TC-148 | CLI | do_send builds payload | channels+message | correct payload | U | L |
| TC-149 | CLI | do_status formats | valid body | formatted output | U | L |
| TC-150 | CLI | config_check mock mode | MOCK_MODE=true | warning printed | U | L |

---

## 5. Notification API Tests

### 5.1 Successful requests
- Valid SMS request → 202, channel `queued` (TC-103).
- Valid WhatsApp request → 202, channel `queued` (TC-102).
- Valid Email request → 202, channel `queued` (TC-104).
- Multi-channel fan-out → 202 with one `queued` entry per channel (TC-105).
- Valid event envelope → 202 (TC-122).

### 5.2 Validation failures (all expect 4xx)
- Missing / empty / whitespace message (TC-110, TC-111, TC-112).
- Missing / invalid / empty contact (TC-115, TC-116, TC-117).
- Invalid / duplicate / empty channels (TC-106–TC-109).
- Message longer than 4096 (TC-113).

### 5.3 Error scenarios
- Provider auth failure → channel `failed` with reason (TC-043, TC-064, TC-071).
- Provider rejected request / HTTP error (TC-065, TC-066).
- Provider timeout / network failure (TC-057, TC-067).
- Unexpected exception → `failed` "Unexpected error" (TC-044).
- Database/storage failure → message marked failed or API 500 (TC-044 extension).
- Unhandled exception handler → 500 `{"detail":"Internal server error."}` (main.py).

---

## 6. SMS Provider Tests

Cover `VonageSMSProvider` (and `AzureSMSProvider` for the fallback path):

- Success + correct `to`/`from_`/`text` payload (TC-050, TC-051).
- Provider message ID extraction from object and dict responses (TC-050, TC-056).
- Rejected response, auth error, timeout, network error (TC-052, TC-056, TC-057).
- Invalid recipient, missing credentials (TC-053–TC-055).
- Malformed provider response (no `message_uuid`) (TC-056).
- Azure fallback: unsuccessful result, missing from/connection string (TC-070–TC-073).
- **Mocking:** `unittest.mock` on the Vonage `client.messages.send` and Azure `SmsClient`.

---

## 7. WhatsApp Provider Tests

Cover `VonageWhatsAppProvider` and `AzureWhatsAppProvider`:

- Success + correct payload (`from`, `to`, `message_type=text`, `channel=whatsapp`) (TC-058, TC-059).
- E.164 normalization of recipient (TC-060).
- Message UUID extraction (TC-058).
- Auth failure 401 (TC-064), sandbox 403 (TC-065), HTTP 5xx (TC-066).
- Timeout / network error (TC-067).
- Malformed response (no UUID) (TC-068).
- Missing API key / secret / from (TC-061–TC-063).
- Secret never exposed in errors (TC-069).
- Azure path: text/template sends, channel id missing, template missing, receipt errors (TC-085–TC-090).
- **Mocking:** `requests.post` (Vonage), Azure SDK `NotificationMessagesClient`.

---

## 8. Email Provider Tests

Cover `AzureEmailProvider`:

- Success with plain body and subject (TC-074).
- Missing recipient / invalid email (TC-115–TC-117 via API; provider-level invalid).
- Template send (TC-076).
- Attachments: base64, URL download, http rejection, localhost/private IP SSRF, redirects,
  oversized (TC-077–TC-083).
- Provider failure / auth / timeout / network via mocked `EmailClient` (TC-074 extension).
- Missing `AZURE_EMAIL_FROM` (TC-075).
- **Mocking:** `EmailClient.from_connection_string` + `begin_send`, `httpx.stream`.

---

## 9. Configuration Tests

- All required env vars load (TC-001).
- Missing connection string / API key / secret → provider raises `ProviderConfigError` (TC-002, TC-053, TC-054, TC-061, TC-062, TC-073, TC-075).
- Legacy aliases (`AZURE_COMMUNICATION_CONNECTION_STRING`, `AZURE_WHATSAPP_*`) honored (TC-003, TC-004).
- Factory selection driven by config (TC-045–TC-049).
- MOCK_MODE behavior: providers short-circuit to mock results (TC-142).
- Use fake values (`test-api-key`, `test-api-secret`); never real secrets.

---

## 10. Security Tests

- Secrets not returned in API responses (TC-143).
- Secrets redacted in webhook logging via `_redact` (TC-091, TC-144).
- Invalid / missing API key → 401 (TC-013, TC-014, TC-015).
- `AUTH_ENABLED=true` without key → 500 (TC-016).
- SSRF protections on attachment URLs (TC-079–TC-082).
- SQL-injection-style contact strings rejected by validation / parameterized SQL (TC-145).
- HTML escaping in email templates (TC-026).
- Path traversal blocked in template names (TC-027).

---

## 11. Error Handling Tests

Applicable HTTP status codes in this project: **400, 401, 404, 422, 500**.

| Status | When | Test |
| ------ | ---- | ---- |
| 400 | validation_error from `validate_contact` | TC-116, TC-117 |
| 401 | missing/wrong API key | TC-014, TC-015 |
| 404 | status lookup unknown id (v1 + legacy) | TC-130, TC-138 |
| 422 | Pydantic validation failures | TC-106–TC-117 |
| 500 | `AUTH_ENABLED=true` w/o key; unhandled exception handler | TC-016, TC-044 |

Also test:
- Timeout / connection error → `ProviderError` (TC-057, TC-067).
- Unexpected exception → `failed` with "Unexpected error" (TC-044).
- Invalid / empty provider response (TC-056, TC-068).
- Malformed JSON webhook payload → handled without crash (TC-101 extension).
- Missing provider message ID → `ProviderError` (TC-056, TC-068).

---

## 12. Database and Persistence Tests

Use a **temporary SQLite file** (e.g. `:memory:` or `tmp_path`), never production data:

- Create record, update status, retrieve, missing record, group query, list filter
  (TC-028–TC-035).
- Duplicate `message_id` → `IntegrityError` (TC-034).
- `update_status_by_provider_id` persists webhook outcome (TC-030, TC-097, TC-098).
- Error persistence: provider failure stored in `error` column (TC-043, TC-098).
- Database failure path: `get_connection`/`init_db` on a read-only/invalid path
  surfaces an exception the orchestrator records (TC-044 extension).

---

## 13. Notification Lifecycle Tests

```
Request → Validation → Service → Provider → Provider response → Status → Persistence → API response
```

- Successful lifecycle: send → provider returns `sent` → DB `sent` → API 202 (TC-139).
- Validation failure: rejected before queuing, 4xx (TC-106–TC-117).
- Provider failure: DB `failed` with reason (TC-140).
- Provider timeout: DB `failed` (TC-043 extension).
- Persistence failure: exception recorded as `failed` (TC-044).
- Partial failure across channels → group `partial` (TC-141).
- Unexpected exception → `failed` (TC-044).

---

## 14. Edge Case Tests

- Very long message (4096 boundary, >4096) (TC-113, TC-114).
- Empty / whitespace message (TC-111, TC-112).
- Unicode, emojis, newlines, special characters (TC-146).
- Very long recipient (schema max 254) (TC-115 extension).
- Invalid country code / phone normalization variants (TC-017–TC-021).
- Duplicate request / duplicate channel (TC-108).
- Null values → 422 (TC-106 extension).
- Unexpected JSON fields (extra ignored per `extra="ignore"`) (TC-147).
- Extremely large request → validation / size limits (TC-113 extension).

---

## 15. Mocking Strategy

External services are **never** called during automated tests. Mock:

| Target | Mock point |
| ------ | ---------- |
| Vonage SMS SDK | `vonage_provider.VonageSMSProvider` → `vonage_messages.Messages.send` |
| Vonage WhatsApp HTTP | `app.providers.vonage_provider.requests.post` |
| Azure SMS | `azure.communication.sms.SmsClient.from_connection_string` + `.send` |
| Azure Email | `azure.communication.email.EmailClient` + `begin_send` |
| Azure WhatsApp | `azure.communication.messages.NotificationMessagesClient` + `.send` |
| Attachment downloads | `azure_provider.AzureEmailProvider._fetch_as_base64` / `httpx.stream` |
| Time | `datetime`/`time.sleep` (for MOCK delivery + timeout tests) |

Tests must be deterministic and repeatable — use fixed UUIDs, fake responses, and
`patch.dict("os.environ", ...)` for config with `get_settings.cache_clear()`.

---

## 16. Code Coverage Requirements

Target (measured with `coverage.py`):

| Metric | Target |
| ------ | ------ |
| Overall | **>= 90%** |
| Statements | >= 90% |
| Branches | >= 90% |
| Functions | >= 90% |
| Lines | >= 90% |

**Limitation:** `coverage.py` reports `stmt/miss`, `branch/miss`, `part_branch`,
`func/miss`, and `lines`/`miss` — all five metrics above are supported.

---

## 17. Coverage Exclusions

Only genuinely non-application files are excluded, each with a reason:

| Path | Reason |
| ---- | ------ |
| `venv/`, `.venv/` | third-party packages (outside coverage by default) |
| `*.pyc`, `__pycache__/` | bytecode caches |
| `notification_service.py` (CLI) | interactive/process-control entry point; core logic is covered via API (kept **out** of exclusion only if CLI functions are unit-tested — TC-148..TC-150; otherwise document partial coverage) |
| `run.sh`, `cli.sh` | shell scripts, not Python |
| `templates/`, `examples/`, `*.db` | non-code assets |

**Not excluded:** `app/` application logic (providers, orchestrator, routers, database,
validation, config, auth, templates, schemas). Business logic must reach 90% — no
exclusions used merely to hit the target.

---

## 18. Test Directory Structure

Proposed (adapt existing standalone tests into a `tests/` package):

```text
tests/
├── conftest.py                 # fixtures: TestClient, temp DB, env patching, mock providers
├── unit/
│   ├── test_config.py
│   ├── test_validation.py
│   ├── test_templates.py
│   ├── test_database.py
│   ├── test_orchestrator.py
│   ├── test_auth.py
│   ├── providers/
│   │   ├── test_vonage_sms.py
│   │   ├── test_vonage_whatsapp.py
│   │   ├── test_azure_sms.py
│   │   ├── test_azure_email.py
│   │   └── test_azure_whatsapp.py
│   └── test_factory.py
├── integration/
│   ├── test_lifecycle.py
│   └── test_webhooks_db.py
├── api/
│   ├── test_send.py
│   ├── test_event.py
│   ├── test_status.py
│   ├── test_legacy.py
│   └── test_health.py
└── security/
    └── test_security.py
```

Existing root-level test scripts (`test_webhooks.py`, `test_vonage_whatsapp.py`,
`test_vonage_sms.py`, `test_azure_whatsapp.py`, `test_azure_whatsapp_template.py`)
should be migrated into this structure during implementation, keeping their working
assertions.

---

## 19. Test Naming Convention

Use `pytest` with the convention:

```text
test_<component>_<scenario>_<expected_result>
```

Examples:

```python
test_whatsapp_provider_valid_request_returns_message_id
test_whatsapp_provider_invalid_credentials_raises_error
test_notification_api_invalid_channel_returns_422
test_validate_contact_invalid_phone_raises_error
test_database_update_status_persists_provider_id
test_webhooks_failed_event_stores_error_reason
```

Test files: `test_*.py`. Test classes (if used): `Test<Component>`.

---

## 20. Test Execution

Prerequisites (to be installed during implementation):

```bash
venv/bin/pip install pytest pytest-cov
```

Commands:

```bash
# Run all tests
venv/bin/python -m pytest tests/ -v

# Run unit tests
venv/bin/python -m pytest tests/unit/ -v

# Run integration tests
venv/bin/python -m pytest tests/integration/ -v

# Run API tests
venv/bin/python -m pytest tests/api/ -v

# Generate coverage report (terminal)
venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing

# Generate HTML coverage report
venv/bin/python -m pytest tests/ --cov=app --cov-report=html
# open htmlcov/index.html

# Enforce 90% coverage (fail if below)
venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing \
  --cov-fail-under=90
```

Note: existing standalone scripts can still run directly until migrated:
`venv/bin/python test_webhooks.py`, `venv/bin/python test_vonage_whatsapp.py`, etc.

---

## 21. CI/CD Coverage Gate

No CI configuration exists in the repository yet. Recommended (to be added later,
**not** implemented in this task):

```yaml
# .github/workflows/tests.yml (proposed)
- run: venv/bin/pip install -r requirements.txt pytest pytest-cov
- run: venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=90
```

The gate fails the build when **overall coverage < 90%**.

---

## 22. Test Implementation Order

1. Configuration tests (`app/config.py`, factory selection).
2. Validation tests (`app/validation.py`, schema constraints).
3. Utility tests (`_normalize_phone`, templates, `_redact`, `_extract_failure`).
4. Provider unit tests (Vonage SMS/WhatsApp, Azure SMS/Email/WhatsApp) — all mocked.
5. Service tests (orchestrator: dispatch, status, timeout).
6. API tests (send, event, status, legacy, health) via `TestClient`.
7. Integration tests (route → provider → DB; webhook → DB).
8. Error-path tests (4xx/5xx, provider failures).
9. Edge-case tests (Section 14).
10. Coverage-gap tests (run coverage, add missing cases).
11. Full regression tests (existing functionality).
12. Verify 90% coverage gate.

---

## 23. Definition of Done

Testing implementation is complete only when:

- [ ] All critical functions/classes have tests.
- [ ] All API endpoints have tests (v1 + legacy + webhook + health).
- [ ] All notification channels have tests (SMS, WhatsApp, Email).
- [ ] Positive and negative scenarios are covered.
- [ ] External providers are mocked; no real SMS/WhatsApp/email sent during tests.
- [ ] Error handling is tested (400/401/404/422/500 + provider/network errors).
- [ ] Edge cases are tested (Section 14).
- [ ] Security tests pass (secrets hidden, SSRF, injection, escaping).
- [ ] Overall coverage **>= 90%**.
- [ ] Statements >= 90%, Branches >= 90%, Functions >= 90%, Lines >= 90%.
- [ ] Coverage exclusions are justified and minimal.
- [ ] Existing application functionality still passes (regression).
- [ ] Test suite runs consistently and deterministically.
- [ ] `--cov-fail-under=90` gate passes locally.

---

## Coverage measurement command (reference)

```bash
venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=90
```
