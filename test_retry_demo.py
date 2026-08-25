"""
Retry demo - runs in-process (no separate server needed).
Shows retry with exponential backoff working end-to-end.
Usage: source venv/bin/activate && python test_retry_demo.py
"""
import json
import time
from app.main import app
from fastapi.testclient import TestClient
from app.providers.azure_provider import AzureEmailProvider
from app.providers.base import ProviderTransientError

client = TestClient(app)

original_send = AzureEmailProvider.send
call_count = 0

def failing_send(self, contact, message, subject="Notification"):
    global call_count
    call_count += 1
    if call_count <= 2:
        print(f"  [PROVIDER] Attempt {call_count}: SIMULATING TIMEOUT -> retrying...")
        raise ProviderTransientError("Simulated network timeout")
    print(f"  [PROVIDER] Attempt {call_count}: SUCCESS")
    return original_send(self, contact, message, subject)

AzureEmailProvider.send = failing_send

print("=" * 60)
print("  RETRY DEMO: Email fails 2x, succeeds on 3rd try")
print("=" * 60)
print()

print("Step 1: Patching email provider to fail on first 2 attempts...")
print()

print("Step 2: Sending request...")
start = time.time()
r = client.post("/api/v1/notifications/send", json={
    "channels": [{"channel": "email", "contact": "test@example.com"}],
    "message": "Retry demo message"
})
data = r.json()
group_id = data["message_id"]
print(f"  Group ID: {group_id}")
print(f"  Initial status: {data['status']}")
print()

print("Step 3: Waiting for retries to complete...")
time.sleep(5)
print()

print("Step 4: Checking final status...")
r = client.get(f"/api/v1/notifications/{group_id}/status")
result = r.json()
elapsed = time.time() - start
print(f"  Overall status: {result['status']}")
print(f"  Total time: {elapsed:.1f}s")
for ch in result["channels"]:
    print(f"  {ch['channel']:10s} -> {ch['status']} (attempts: {ch['attempt_count']}, error: {ch['error']})")
print()

AzureEmailProvider.send = original_send

print("=" * 60)
print("  WHAT HAPPENED:")
print("  Attempt 1: Provider raised timeout error -> waited 0.5s -> retry")
print("  Attempt 2: Provider raised timeout error -> waited 1.0s -> retry")
print("  Attempt 3: Provider succeeded -> status: delivered")
print(f"  attempt_count: {result['channels'][0]['attempt_count']} (shows total tries)")
print("=" * 60)
print()

print("Now testing in Postman - run these:")
print()

print("--- DEMO: Permanent Error (no retry) ---")
r = client.post("/api/v1/notifications/send", json={
    "channels": [{"channel": "email", "contact": "bad-email"}],
    "message": "test"
})
print(f"POST /api/v1/notifications/send")
print(f"Body: {{\"channels\": [{{\"channel\": \"email\", \"contact\": \"bad-email\"}}], \"message\": \"test\"}}")
print(f"Status: {r.status_code}")
print(f"Response: {json.dumps(r.json(), indent=2)}")
print()

print("--- DEMO: Normal Send (attempt_count: 1) ---")
r = client.post("/api/v1/notifications/send", json={
    "channels": [
        {"channel": "email", "contact": "test@example.com"},
        {"channel": "sms", "contact": "+918660556303"},
        {"channel": "whatsapp", "contact": "+918660556303"}
    ],
    "message": "Normal send"
})
data = r.json()
print(f"POST /api/v1/notifications/send")
print(f"Status: {r.status_code}")
time.sleep(2)
r = client.get(f"/api/v1/notifications/{data['message_id']}/status")
result = r.json()
for ch in result["channels"]:
    print(f"  {ch['channel']:10s} -> {ch['status']} (attempts: {ch['attempt_count']})")
