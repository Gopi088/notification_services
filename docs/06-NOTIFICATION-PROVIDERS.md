# 06 — Notification Providers

## 6.1 Provider Abstraction

The provider layer isolates all external API details behind a stable interface so
that the core notification service, queue, and workers never change when a
provider changes or a new channel is added.

The existing `app/providers/base.py::NotificationProvider` ABC is the foundation;
the spec below extends it with lifecycle and status operations.

**Interface (conceptual)**

```python
class NotificationProvider(ABC):
    name: str
    def send(self, contact, message, **kwargs) -> ProviderResult: ...
    def send_with_template(self, contact, message, template_name, language, params) -> ProviderResult: ...
    def send_delivery(self, payload, data=None) -> ProviderResult: ...
    def validate(self, contact) -> None: ...
    def get_status(self, provider_message_id) -> ProviderStatus: ...
    def is_retryable(self, error) -> bool: ...
```

`ProviderResult` carries `provider_message_id` + status (`submitted`). Errors are
raised as `ProviderError` (with `retryable` classification) or
`ProviderConfigError` (permanent config problem).

## 6.2 Factory

`app/providers/factory.py::get_provider(channel)` selects the provider from config:

| Channel | Preferred | Fallback |
| ------- | --------- | -------- |
| `sms` | `VonageSMSProvider` | `AzureSMSProvider` |
| `whatsapp` | `VonageWhatsAppProvider` | `AzureWhatsAppProvider` |
| `email` | `AzureEmailProvider` | — |

Adding a channel = add a provider class + register it in the factory; the
orchestrator/worker need no change (this is already the current design).

## 6.3 SMS

| Aspect | Vonage SMS | Azure SMS |
| ------ | ---------- | --------- |
| Auth | Basic (`VONAGE_API_KEY` + `VONAGE_API_SECRET`) | Connection string |
| Endpoint | `POST https://api.nexmo.com/v1/messages` (SDK `Sms`) | `SmsClient.send` |
| Request | `{to, from, text}` | `{from, to, message}` |
| Response | `message_uuid` | `message_id` + `successful` flag |
| Timeout | SDK default; configure 10–30 s | SDK default |
| Retryable errors | 429, 5xx, network, timeout | 429, 5xx, network |
| Non-retryable | 4xx (bad recipient/sender/credits), validation | `successful=false`, 4xx |
| Rate limits | Provider-dashboard quota; enforce via Redis | Azure quota |

## 6.4 WhatsApp

| Aspect | Vonage Sandbox | Azure ACS |
| ------ | -------------- | --------- |
| Auth | Basic (API key/secret) | Connection string |
| Endpoint | `POST https://messages-sandbox.nexmo.com/v1/messages` | `NotificationMessagesClient.send` |
| Request | `{from, to, message_type, text, channel}` | `TextNotificationContent` / `TemplateNotificationContent` |
| Response | `message_uuid` | `receipt.message_id` |
| 24h rule | Free text only within 24h session; template otherwise | same |
| Templates | (Sandbox text only; production uses template) | `send_template()` with bindings |
| Retryable | 429, 5xx, network, timeout | 429, 5xx, network |
| Non-retryable | 401 auth, 403 sandbox not allow-listed, 4xx invalid | 4xx, template errors |
| Status via webhook | Delivery receipt webhook | `/api/v1/whatsapp/webhook` (Event Grid) |

## 6.5 Email

| Aspect | Azure Email |
| ------ | ----------- |
| Auth | Connection string |
| Endpoint | `EmailClient.begin_send` |
| Request | sender, recipients (to/cc/bcc), content (subject, plainText, html), replyTo, attachments |
| Response | `message_id` |
| Attachments | url (SSRF-guarded) or content_base64 |
| Templates | local HTML templates (`templates/email/*.html`) |
| Retryable | 429, 5xx, network, timeout |
| Non-retryable | 4xx, invalid sender/recipient, oversized attachment |

## 6.6 Error Mapping

| Provider response | Mapped to | Retryable |
| ----------------- | --------- | --------- |
| HTTP 400 / 422 | `ProviderError` "invalid request" | No |
| HTTP 401 / 403 | `ProviderError` "auth/credentials/allow-list" | No |
| HTTP 404 | `ProviderError` "recipient not found" | No |
| HTTP 408 / timeout | `ProviderError` "timeout" | Yes |
| HTTP 409 | `ProviderError` "conflict" | No |
| HTTP 429 | `ProviderError` "rate limited" | Yes (respect `Retry-After`) |
| HTTP 5xx | `ProviderError` "provider unavailable" | Yes |
| Network / connection | `ProviderError` "network" | Yes |
| Missing message id in response | `ProviderError` "no id" | Depends (provider may have accepted → idempotency) |

## 6.7 Provider Timeout & Circuit Breaking

- Each provider call wraps a client timeout (default 30 s).
- A lightweight circuit breaker (per provider) opens after N consecutive
  failures, short-circuits for a cooldown window, and prevents hammering a
  dead provider. Counts toward observability metrics.
- Provider rate-limit awareness: parse `Retry-After`/quota headers and back off.

## 6.8 Adding Future Channels

New channels (Push, Telegram, Slack) implement the same ABC and register in the
factory. No core changes. The queue stream key, worker concurrency config, and
rate-limit buckets are channel-namespaced, so they work unchanged.