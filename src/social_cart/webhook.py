"""Meta webhook: validate first, then enqueue; provider work runs in the worker."""
import hashlib
import hmac
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from .state import connect, enqueue

app = FastAPI()

@app.get("/healthz")
def health():
    return {"ok": True}

@app.get("/instagram/webhook")
async def verify(request: Request):
    expected = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")
    q = request.query_params
    valid = expected and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", "").encode(), expected.encode())
    if not valid or not q.get("hub.challenge"):
        return PlainTextResponse("forbidden", 403)
    return PlainTextResponse(q["hub.challenge"])

@app.post("/instagram/webhook")
async def receive(request: Request):
    secret = os.getenv("INSTAGRAM_APP_SECRET", "")
    if not secret:
        return JSONResponse({"error": "not configured"}, 503)
    try:
        size = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return JSONResponse({"error": "invalid length"}, 400)
    if size < 0 or size > 1048576:
        return JSONResponse({"error": "too large"}, 413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1048576:
            return JSONResponse({"error": "too large"}, 413)
    raw = bytes(body)
    supplied = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return JSONResponse({"error": "forbidden"}, 403)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("entry", []), list):
            raise ValueError
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid payload"}, 400)
    db_path = os.getenv("SOCIAL_CART_DB", "")
    if not db_path:
        return JSONResponse({"error": "not configured"}, 503)
    count = 0
    with connect(db_path) as db:
        for entry in data.get("entry", []):
            if not isinstance(entry, dict):
                continue
            for event in entry.get("messaging", []) or []:
                if isinstance(event, dict) and isinstance(event.get("message"), dict) and isinstance(event.get("sender"), dict):
                    count += int(enqueue(db, event))
    return {"queued": count}
