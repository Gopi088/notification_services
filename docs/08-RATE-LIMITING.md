# 08 — Throttling & Rate Limiting

## 8.1 Purpose

Protect provider quotas, prevent abuse, and shed load during bursts. Rate
limiting is enforced in **Redis** (fast, TTL-based) at multiple layers.

## 8.2 Algorithm

**Sliding window counter** (approximate) is the primary algorithm — it allows
bursts up to the limit while bounding them, uses O(1) Redis ops, and avoids
token-bucket clock issues.

- For strict per-recipient fairness, **sliding window log** can be used (more
  memory) — selected per bucket type.

Alternative: fixed window is simpler but allows 2x bursts at boundaries; token
bucket allows exact sustained rate. Default = sliding window counter.

## 8.3 Bucket Types, Keys, Limits

| Bucket | Redis key | Default limit | TTL |
| ------ | --------- | ------------- | --- |
| Per API key (send) | `rl:key:{api_key_id}:send` | 100 req / min | 60 s |
| Per API key (status) | `rl:key:{api_key_id}:status` | 300 req / min | 60 s |
| Per recipient | `rl:recipient:{normalized_phone}` | 20 sends / hour | 3600 s |
| Per channel (send) | `rl:channel:{channel}:send` | 500 req / min | 60 s |
| Per provider | `rl:provider:{provider}:send` | matches provider quota (from config) | 60 s |
| Worker egress | `rl:worker:{provider}:send` | per-worker rate guard | 60 s |

Keys are namespaced `rl:` and expire automatically (no cleanup job needed).

## 8.4 Enforcement Points

1. **API layer** (per API key, per channel, per recipient): checked in the send
   endpoint before enqueue. Exceeded ⇒ `429` + `Retry-After` header; message not enqueued.
2. **Worker layer** (per provider): checked before each provider call; on limit,
   the worker backs off using the provider's `Retry-After`/quota rather than
   burning attempts.

## 8.5 Behavior When Limit Exceeded

**API:**

```
HTTP/1.1 429 Too Many Requests
Retry-After: 37
{
  "success": false,
  "error": {"code": "rate_limited",
            "message": "Send limit exceeded for this key/recipient/channel.",
            "field": null}
}
```

**Worker:** requeue to retry stream with backoff honoring `Retry-After`; do not
consume an attempt counter for provider throttling alone (configurable).

**Queue:** if a channel's queue depth exceeds a threshold (backlog), new API
enqueues for that channel return `429` with `Retry-After` (backpressure).

## 8.6 Burst Scenarios

| Burst | Behavior |
| ----- | -------- |
| 100 requests sudden | Within per-key/per-channel limits (100/min default) — all accepted, queued, processed. |
| 10,000 requests sudden | Exceeds per-key limit → 429 for excess. Per-channel 500/min caps provider egress. Queue builds up; workers drain at provider rate. No provider overload. |
| 1,000,000 requests sudden | Same as above at scale: API is horizontally scalable, Redis counters shardable, workers capped by provider rate. Non-limit-exceeding requests queue; excess rejected fast (429) without loading DB. |

Design intent: **reject fast at the edge** (Redis), **absorb acceptable bursts in
the queue**, and **never exceed provider rate limits**.

## 8.7 Redis Failure Behavior

- If Redis is unavailable, rate limiting **fails open** (allow) so the service
  keeps working, with an alert and metric `ratelimit.redis_down`. Provider
  quotas still bound actual egress at the worker.

## 8.8 Headers

Responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset` on `/api/v1/*` when rate limiting is active.