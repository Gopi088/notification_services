"""Tests for the endurance test and dummy provider (load_tests)."""
import json


def test_mask_redacts_secrets():
    from load_tests.endurance_test import _mask

    payload = {
        "client_id": "user",
        "client_secret": "super-secret",
        "channels": [{"channel": "sms", "contact": "+9198000000001"}],
        "message": "hello",
    }
    masked = _mask(payload)
    assert masked["client_secret"] == "***"
    assert masked["client_id"] == "user"
    assert masked["channels"][0]["contact"] == "+9198000000001"


def test_validate_ok():
    from load_tests.endurance_test import _validate

    result = {"ok": True, "parsed": {"message_id": "m1", "channels": [], "status": "queued"}}
    assert _validate(202, ["message_id", "channels"], result) == ""


def test_validate_missing_fields():
    from load_tests.endurance_test import _validate

    result = {"ok": True, "parsed": {"message_id": "m1"}}
    err = _validate(202, ["message_id", "channels"], result)
    assert "channels" in err


def test_validate_bad_status():
    from load_tests.endurance_test import _validate

    result = {"ok": False, "status": 500, "parsed": None}
    err = _validate(202, ["message_id"], result)
    assert "500" in err


def test_build_request_shape():
    from load_tests.endurance_test import _build_request

    req = _build_request(7, "sms")
    assert req["channels"][0]["channel"] == "sms"
    assert "endurance-7" in req["message"]
    assert "+9198" in req["channels"][0]["contact"]


def test_dummy_provider_endpoints():
    """The dummy provider returns provider-style responses without real sends."""
    from fastapi.testclient import TestClient
    from load_tests.dummy_provider import app

    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        r = c.post("/2010-04-01/Accounts/ACdummy/Messages.json",
                   data={"To": "+9198000000001", "From": "+17372508034", "Body": "hi"})
        assert r.status_code == 201
        body = r.json()
        assert body["sid"].startswith("SM")
        assert body["status"] == "queued"

        r = c.post("/2010-04-01/Accounts/ACdummy/Messages.json",
                   data={"To": "whatsapp:+9198000000001", "From": "whatsapp:+17372508034", "Body": "hi"})
        assert r.json()["sid"].startswith("MM")  # WhatsApp SID prefix

        assert c.get("/2010-04-01/Accounts/ACdummy/Messages/SM123.json").json()["status"] == "delivered"
        assert c.post("/v1/messages", json={"channel": "sms"}).status_code == 202
        assert c.post("/emails:send").status_code == 202


def test_endurance_record_validation():
    """A failed request is recorded with PASS/FAIL and an error reason."""
    from load_tests.endurance_test import Recorder

    r = Recorder(None, 15.0)
    r.record({"pass": True, "error": "", "actual_status": 202, "latency_ms": 10.0,
              "request": {}, "actual_body": {}})
    r.record({"pass": False, "error": "http_status=500 expected=202", "actual_status": 500,
              "latency_ms": 20.0, "request": {}, "actual_body": {}})
    snap = r.snapshot()
    assert snap["total"] == 2
    assert snap["ok"] == 1
    assert snap["failures"] == 1
    assert "http_status=500" in " ".join(snap["fail_reasons"].keys())
    r.close()