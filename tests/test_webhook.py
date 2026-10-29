import hashlib
import hmac
import json
from fastapi.testclient import TestClient
from social_cart.webhook import app

def test_verified_ingress_deduplicates_without_provider_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CART_DB", str(tmp_path / "inbox.db"))
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-secret")
    monkeypatch.setenv("INSTAGRAM_VERIFY_TOKEN", "test-verify")
    client = TestClient(app)
    assert client.get("/instagram/webhook", params={"hub.mode":"subscribe","hub.verify_token":"test-verify","hub.challenge":"123"}).text == "123"
    event = {"entry":[{"messaging":[{"sender":{"id":"123"}, "message":{"mid":"synthetic-mid", "text":"https://www.instagram.com/reel/ABC123/"}}]}]}
    raw = json.dumps(event).encode()
    sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    assert client.post("/instagram/webhook", content=raw).status_code == 403
    assert client.post("/instagram/webhook", content=raw, headers={"x-hub-signature-256":sig}).json()["queued"] == 1
    assert client.post("/instagram/webhook", content=raw, headers={"x-hub-signature-256":sig}).json()["queued"] == 0
    assert (tmp_path / "inbox.db").stat().st_mode & 0o777 == 0o600

def test_unconfigured_webhook_fails_closed(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    assert TestClient(app).post("/instagram/webhook", content=b"{}").status_code == 503
