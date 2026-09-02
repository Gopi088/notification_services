#!/usr/bin/env python3
"""
Local dummy provider server for endurance/load testing.

Simulates Twilio, Vonage, and Azure provider endpoints so the notification
service can be pointed at this server instead of real paid providers. No real
SMS/WhatsApp/Email messages are sent.

Usage:
    python3 load_tests/dummy_provider.py [--port 9090] [--latency-ms 50]

When the notification service is started with:
    TWILIO_API_BASE_URL=http://127.0.0.1:9090
    MOCK_MODE=false
all Twilio SMS/WhatsApp requests go to this dummy server instead of Twilio.
"""
import argparse
import asyncio
import json
import uuid
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Dummy Provider", version="1.0.0")

LATENCY_MS = 50


def _sid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16].upper()}"


async def _maybe_latency():
    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000.0)


# ---- Twilio-style endpoints (SMS + WhatsApp) ----
@app.post("/2010-04-01/Accounts/{account_sid}/Messages.json")
async def twilio_send(account_sid: str, request: Request) -> JSONResponse:
    await _maybe_latency()
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v for k, v in parse_qsl(raw, keep_blank_values=True)}
    to = form.get("To", "")
    is_whatsapp = to.startswith("whatsapp:")
    sid_prefix = "MM" if is_whatsapp else "SM"
    sid = _sid(sid_prefix)
    return JSONResponse({
        "sid": sid,
        "status": "queued",
        "to": to,
        "from": form.get("From", ""),
        "body": form.get("Body", "")[:80],
    }, status_code=201)


@app.get("/2010-04-01/Accounts/{account_sid}/Messages/{message_sid}.json")
async def twilio_status(account_sid: str, message_sid: str) -> JSONResponse:
    await _maybe_latency()
    return JSONResponse({
        "sid": message_sid,
        "status": "delivered",
        "account_sid": account_sid,
    })


# ---- Vonage-style endpoints ----
@app.post("/v1/messages")
async def vonage_send(request: Request) -> JSONResponse:
    await _maybe_latency()
    body = await request.json()
    msg_uuid = str(uuid.uuid4())
    return JSONResponse({
        "message_uuid": msg_uuid,
        "status": "accepted",
        "channel": body.get("channel", "sms"),
    }, status_code=202)


# ---- Azure-style email endpoint ----
@app.post("/emails:send")
async def azure_email_send(request: Request) -> JSONResponse:
    await _maybe_latency()
    op_id = str(uuid.uuid4())
    return JSONResponse(
        {"id": op_id, "status": "Queued"},
        status_code=202,
        headers={
            "Operation-Location": f"http://127.0.0.1:9090/emails/operations/{op_id}?api-version=2025-09-01",
            "Retry-After": "3",
        },
    )


@app.get("/emails/operations/{op_id}")
async def azure_email_operation(op_id: str, request: Request) -> JSONResponse:
    await _maybe_latency()
    return JSONResponse({"id": op_id, "status": "Succeeded"})


# ---- Health ----
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "latency_ms": LATENCY_MS})


def main():
    global LATENCY_MS
    parser = argparse.ArgumentParser(description="Dummy provider server for endurance testing")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--latency-ms", type=int, default=50,
                        help="Simulated provider latency (default 50ms)")
    args = parser.parse_args()
    LATENCY_MS = args.latency_ms
    print(f"Dummy provider on {args.host}:{args.port} latency={LATENCY_MS}ms")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()