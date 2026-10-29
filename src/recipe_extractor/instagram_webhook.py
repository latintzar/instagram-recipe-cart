"""Inbound Instagram Messaging webhook — receive a shared reel, get a recipe.

This is the "people send us a reel in a DM" connection. It implements the Meta
Instagram Messaging webhook so that when someone *shares a reel into your
Instagram inbox*, you resolve it to a downloadable URL and run it through
:func:`recipe_extractor.extract.extract_recipe`.

Two layers:

* **Framework-agnostic helpers** (no web framework needed):
    - :func:`verify_subscription` — the GET handshake (hub.challenge echo).
    - :func:`verify_signature`    — the ``X-Hub-Signature-256`` HMAC check.
    - :func:`iter_messaging_events` — normalise a webhook POST body into simple
      message dicts, each already carrying a resolved ``reel_url`` when present.
    - :func:`send_message`        — reply to the sender via the Send API.

* **Optional FastAPI router** (:func:`make_router`) that wires the above into
  ``GET/POST /instagram/webhook`` and calls your ``on_reel`` callback with the
  extracted recipe. Requires ``pip install "recipe-extractor[service]"``.

Env vars (see .env.example):
    INSTAGRAM_VERIFY_TOKEN        — your chosen verify token (GET handshake)
    INSTAGRAM_APP_SECRET          — Meta app secret (HMAC signature check)
    INSTAGRAM_PAGE_ACCESS_TOKEN   — Graph token (reel resolution + Send API)
    INSTAGRAM_BUSINESS_ID         — IG user id (Send API, EAAB token flavour only)

Meta setup, briefly: create a Meta app with Instagram messaging, subscribe the
webhook to ``messages``, point the callback URL at ``…/instagram/webhook`` and
use the same value for the verify token field and ``INSTAGRAM_VERIFY_TOKEN``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Callable, Iterator

import httpx

from .instagram_cdn import (
    GRAPH_FB_BASE,
    GRAPH_IG_BASE,
    attachment_reel_video_id,
    resolve_ig_reel_video_id_to_permalink,
    resolve_ig_share_url_from_message_mid,
)

logger = logging.getLogger("recipe_extractor.instagram_webhook")

_IG_REPLY_LIMIT = 1000
_PERMALINK_RE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/[A-Za-z0-9_-]+", re.IGNORECASE)
_PRIVATE_MEDIA_HOSTS = ("cdninstagram.com", "fbcdn.net", "instagram.f")


# --- Framework-agnostic helpers ----------------------------------------------


def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    """Meta GET subscription handshake. Return ``challenge`` to echo, else None.

    Your GET route should return this string with status 200 when non-None,
    or 403 otherwise.
    """
    expected = os.getenv("INSTAGRAM_VERIFY_TOKEN")
    if mode == "subscribe" and expected and token == expected and challenge:
        return challenge
    logger.warning(
        "ig_webhook: verify failed mode=%s token_match=%s has_challenge=%s",
        mode,
        token == expected,
        bool(challenge),
    )
    return None


def verify_signature(header: str | None, raw_body: bytes) -> bool:
    """Verify ``X-Hub-Signature-256: sha256=<hmac>`` against ``INSTAGRAM_APP_SECRET``.

    A missing secret or signature is rejected.
    """
    secret = os.getenv("INSTAGRAM_APP_SECRET")
    if not secret:
        return False
    if not header or not header.startswith("sha256="):
        logger.warning("ig_webhook: missing/malformed x-hub-signature-256")
        return False
    expected = header.removeprefix("sha256=").strip()
    computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, computed):
        logger.warning("ig_webhook: signature mismatch — possible replay/spoofing")
        return False
    return True


def _is_private_media_host(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in _PRIVATE_MEDIA_HOSTS)


def iter_messaging_events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield normalised message dicts from a Meta webhook payload.

    Each yielded dict::

        {
          "sender_id": str,
          "text": str,                    # any text the user typed
          "mid": str | None,              # message id (for dedupe / shares lookup)
          "is_echo": bool,                # True for your own outgoing messages
          "reel_url": str | None,         # RESOLVED, extractable reel URL if shared
          "attachment_urls": list[str],   # other attachment URLs
          "voice_attachment_urls": list[str],
        }

    Reel resolution order (the hard-won part):
      1. An ``ig_reel``/``reel`` attachment carries a ``reel_video_id`` -> resolve
         via the Graph media node to an ``instagram.com/reel/…`` permalink.
      2. Otherwise, for non-echo messages, expand the message ``mid`` into its
         ``shares`` object and pull the public URL / lookaside CDN link.
    A ``lookaside.fbsbx.com`` CDN link is itself extractable (it serves the MP4
    directly), so it's a valid ``reel_url``.
    """
    for entry in payload.get("entry", []) or []:
        for ev in entry.get("messaging", []) or []:
            sender = (ev.get("sender") or {}).get("id")
            msg = ev.get("message") or {}
            mid = msg.get("mid")
            is_echo = bool(msg.get("is_echo"))
            text = (msg.get("text") or "").strip()

            attachment_urls: list[str] = []
            voice_attachment_urls: list[str] = []
            reel_url: str | None = None

            for att in msg.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                kind = str(att.get("type") or "").lower()
                payload_obj = att.get("payload") if isinstance(att.get("payload"), dict) else {}
                url = payload_obj.get("url") if isinstance(payload_obj, dict) else None
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    if kind in ("audio", "ig_audio"):
                        voice_attachment_urls.append(url)
                    else:
                        attachment_urls.append(url)
                if kind in ("ig_reel", "reel") and reel_url is None:
                    rid = attachment_reel_video_id(payload_obj)
                    if rid:
                        reel_url = resolve_ig_reel_video_id_to_permalink(rid)
                        if reel_url:
                            logger.info("ig_webhook: resolved reel_video_id=%s -> permalink", rid)

            # Fallback: expand the message id into its shares object. Skip echoes
            # — the Graph API rejects /shares lookups on your own outgoing mids,
            # and echoes never contain user-shared reels anyway.
            if reel_url is None and mid and not is_echo:
                reel_url = resolve_ig_share_url_from_message_mid(str(mid))

            # A lookaside CDN attachment is directly extractable — promote it.
            if reel_url is None:
                for u in attachment_urls:
                    if "lookaside.fbsbx.com" in u.lower() or "ig_messaging_cdn" in u.lower():
                        reel_url = u
                        break

            # A pasted permalink in the text is also extractable.
            if reel_url is None and text:
                m = _PERMALINK_RE.search(text)
                if m:
                    reel_url = "https://www." + m.group(0).split("www.", 1)[-1]

            if not sender:
                continue
            if not (text or attachment_urls or reel_url or voice_attachment_urls):
                continue

            yield {
                "sender_id": str(sender),
                "text": text,
                "mid": mid,
                "is_echo": is_echo,
                "reel_url": reel_url,
                "attachment_urls": attachment_urls,
                "voice_attachment_urls": voice_attachment_urls,
            }


def _graph_messages_endpoint(token: str, ig_user_id: str | None) -> str:
    if token.startswith(("IGAA", "IGQ")):
        return f"{GRAPH_IG_BASE}/me/messages"
    return f"{GRAPH_FB_BASE}/{ig_user_id or 'me'}/messages"


def send_message(recipient_id: str, text: str) -> dict[str, Any]:
    """Reply to a sender via the Instagram Graph Send API.

    Returns the parsed JSON on success or ``{"error": …}`` on failure — never
    raises, because callers are webhook handlers that must still return 200
    (Meta retries on anything else).
    """
    token = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
    ig_user_id = os.getenv("INSTAGRAM_BUSINESS_ID")
    if not token:
        logger.error("ig_webhook: send_message missing INSTAGRAM_PAGE_ACCESS_TOKEN")
        return {"error": "missing_credentials"}
    url = _graph_messages_endpoint(token, ig_user_id)
    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": text[:_IG_REPLY_LIMIT]},
        "messaging_type": "RESPONSE",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, params={"access_token": token}, json=body)
        if resp.status_code >= 400:
            logger.error("ig_webhook: send HTTP %d body=%s", resp.status_code, resp.text[:400])
            return {"error": f"http_{resp.status_code}", "detail": resp.text[:400]}
        data = resp.json()
        logger.info("ig_webhook: sent recipient=%s mid=%s", recipient_id, data.get("message_id"))
        return data
    except Exception as exc:  # noqa: BLE001
        logger.exception("ig_webhook: send_message network error recipient=%s", recipient_id)
        return {"error": "send_failed", "detail": str(exc)}


# --- Optional FastAPI router -------------------------------------------------

OnReel = Callable[[str, dict[str, Any]], Any]
"""Callback signature: ``on_reel(sender_id, extraction_result) -> str | None``.

``extraction_result`` is whatever :func:`extract_recipe` returned. Return a
string to auto-reply that text to the sender, or None to stay silent.
"""


def make_router(
    *,
    on_reel: OnReel | None = None,
    auto_reply: bool = False,
    path: str = "/instagram/webhook",
):
    """Build a FastAPI ``APIRouter`` implementing the full inbound flow.

        from fastapi import FastAPI
        from recipe_extractor.instagram_webhook import make_router

        app = FastAPI()
        app.include_router(make_router(auto_reply=True))

    For every shared reel: resolve -> :func:`extract_recipe` -> ``on_reel``
    callback. With ``auto_reply=True`` (and no ``on_reel``), it DMs the
    extracted recipe title back to the sender as a confirmation.
    """
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    from .extract import extract_recipe

    router = APIRouter()

    @router.get(path)
    async def verify(request: Request):  # noqa: ANN202
        p = request.query_params
        challenge = verify_subscription(
            p.get("hub.mode"), p.get("hub.verify_token"), p.get("hub.challenge")
        )
        if challenge is not None:
            return PlainTextResponse(content=challenge, status_code=200)
        return JSONResponse({"error": "verification_failed"}, status_code=403)

    @router.post(path)
    async def webhook(request: Request):  # noqa: ANN202
        raw = await request.body()
        if not verify_signature(request.headers.get("x-hub-signature-256"), raw):
            return JSONResponse({"error": "bad_signature"}, status_code=403)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"status": "ok", "note": "unparseable_payload"})

        handled = 0
        for msg in iter_messaging_events(payload):
            if msg["is_echo"] or not msg["reel_url"]:
                continue
            handled += 1
            try:
                result = extract_recipe(msg["reel_url"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("ig_webhook: extract failed url=%s err=%s", msg["reel_url"], exc)
                continue
            reply: str | None = None
            if on_reel is not None:
                reply = on_reel(msg["sender_id"], result)
            elif auto_reply and result.get("status") == "processed":
                title = result.get("recipe", {}).get("title") or "your recipe"
                reply = f"Saved: {title} ✅"
            if reply:
                send_message(msg["sender_id"], reply)
        return JSONResponse({"status": "ok", "handled": handled})

    return router
