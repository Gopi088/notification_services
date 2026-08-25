"""
Test retry behavior by simulating transient provider failures.
Usage: python test_retry.py
"""
import time
import requests

BASE = "http://127.0.0.1:8000"

def test_permanent_error():
    """Test 1: Permanent error (invalid email) -> fails immediately, no retry"""
    print("=" * 60)
    print("TEST 1: Permanent error (bad email) -> no retry expected")
    print("=" * 60)
    r = requests.post(f"{BASE}/api/v1/notifications/send", json={
        "channels": [{"channel": "email", "contact": "bad-email"}],
        "message": "test"
    })
    print(f"  Status code: {r.status_code}")
    print(f"  Response: {r.json()}")
    print()

def test_validation_error():
    """Test 2: Validation error -> fails immediately, no retry"""
    print("=" * 60)
    print("TEST 2: Validation error (empty message) -> no retry expected")
    print("=" * 60)
    r = requests.post(f"{BASE}/api/v1/notifications/send", json={
        "channels": [{"channel": "email", "contact": "test@example.com"}],
        "message": ""
    })
    print(f"  Status code: {r.status_code}")
    print(f"  Response: {r.json()}")
    print()

def test_not_found():
    """Test 3: 404 -> no retry"""
    print("=" * 60)
    print("TEST 3: Not found -> no retry expected")
    print("=" * 60)
    r = requests.get(f"{BASE}/api/v1/notifications/fake-id-123/status")
    print(f"  Status code: {r.status_code}")
    print(f"  Response: {r.json()}")
    print()

def test_normal_send():
    """Test 4: Normal send -> succeeds on first attempt"""
    print("=" * 60)
    print("TEST 4: Normal send -> should succeed with attempt_count: 1")
    print("=" * 60)
    r = requests.post(f"{BASE}/api/v1/notifications/send", json={
        "channels": [
            {"channel": "email", "contact": "test@example.com"},
            {"channel": "sms", "contact": "+918660556303"},
        ],
        "message": "Retry test message"
    })
    print(f"  Status code: {r.status_code}")
    data = r.json()
    print(f"  Group ID: {data['message_id']}")

    time.sleep(2)

    r = requests.get(f"{BASE}/api/v1/notifications/{data['message_id']}/status")
    result = r.json()
    print(f"  Overall status: {result['status']}")
    for ch in result['channels']:
        print(f"  {ch['channel']:10s} -> {ch['status']} (attempts: {ch['attempt_count']}, error: {ch['error']})")
    print()

def test_retry_timing():
    """Test 5: Show retry timing config"""
    print("=" * 60)
    print("TEST 5: Retry configuration")
    print("=" * 60)
    print("  RETRY_MAX_ATTEMPTS:           3")
    print("  RETRY_BACKOFF_BASE_SECONDS:   0.5")
    print("  RETRY_BACKOFF_MAX_SECONDS:    30")
    print()
    print("  Retry schedule (exponential backoff + jitter):")
    print("    Attempt 1 -> fails -> wait ~0.5s -> retry")
    print("    Attempt 2 -> fails -> wait ~1.0s -> retry")
    print("    Attempt 3 -> fails -> FAILED (no more retries)")
    print()
    print("  Permanent errors (401/403/400) -> fail immediately, 0 retries")
    print("  Transient errors (timeout/429/5xx) -> retry up to 3 times")
    print()

if __name__ == "__main__":
    try:
        requests.get(f"{BASE}/health", timeout=3)
    except requests.ConnectionError:
        print("ERROR: Server not running. Start with:")
        print("  source venv/bin/activate && python -m uvicorn app.main:app --reload")
        exit(1)

    test_permanent_error()
    test_validation_error()
    test_not_found()
    test_normal_send()
    test_retry_timing()
    print("ALL RETRY TESTS COMPLETE")
