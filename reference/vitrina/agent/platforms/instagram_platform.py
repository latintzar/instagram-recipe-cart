"""Instagram DM helpers: stable thread UUID, Mem0 id, and multi-bubble split.

``chat_threads.id`` is a UUID in Supabase — IG senders use a deterministic
UUIDv5 derived from their Meta PSID while ``cart_key`` stays ``ig:{psid}``
for anonymous cart ownership.

Profile lookups use the same ``INSTAGRAM_PAGE_ACCESS_TOKEN`` as Send API;
try ``graph.instagram.com`` first, then ``graph.facebook.com`` for EAAB-style
page tokens.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

from typing import Any

from core import http_client_sync

logger = logging.getLogger(__name__)

# Keep in sync with ``ig_reel_permalink.GRAPH_*`` (avoid importing it — cycles).
_GRAPH_IG_BASE = "https://graph.instagram.com/v23.0"
_GRAPH_FB_BASE = "https://graph.facebook.com/v23.0"

# Deterministic namespace for UUIDv5(thread_id) per Instagram sender.
IG_THREAD_NAMESPACE = uuid.UUID("6b3c8a2e-4e1b-4b2a-9a2b-0b1c2d3e4f5a")

_BREAK = "[BREAK]"

_URL_RE = re.compile(r"https?://[^\s<>\")]+", re.IGNORECASE)


def ig_graph_send_ok(res: Any) -> bool:
    """True when Instagram Graph ``/messages`` returned a success-shaped JSON body.

    :func:`instagram_dm.send_message` maps HTTP/network failures to ``{"error": ...}``.
    Callers must not treat a reply as delivered unless this returns True — otherwise
    Hermes fallbacks never run and shoppers see typing with no bubble (silent failure).
    """
    return isinstance(res, dict) and "error" not in res


def meta_webhook_json(raw: bytes) -> dict[str, Any]:
    """Parse Meta webhook POST body — same bytes used for ``X-Hub-Signature-256``."""
    return json.loads(raw.decode("utf-8"))

# Public Reel / post permalinks the extractor (yt-dlp) can resolve.
_IG_PERMALINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/(?:reel|reels|p|tv)/[^\s]+",
    re.IGNORECASE,
)

# Meta often sends attachment URLs that point at signed CDN buckets — those
# are not fetchable from Railway and are not ``instagram.com/reel/…``.
_CDN_HOST_MARKERS = (
    "cdninstagram.com",
    "fbcdn.net",
    "instagram.f",  # regional edge, e.g. instagram.fsofXX.fna.fbcdn.net
    "fbsbx.com",  # Meta ``lookaside.fbsbx.com/ig_messaging_cdn`` DM attachment URLs
)


def has_instagram_permalink_in_text(text: str) -> bool:
    """True if the shopper pasted a public instagram.com reel/post URL."""
    return bool(_IG_PERMALINK_RE.search(text or ""))


def is_instagram_private_media_host(url: str) -> bool:
    """Heuristic: URL is almost certainly a DM attachment CDN link, not a permalink."""
    u = (url or "").lower()
    return bool(u.startswith(("http://", "https://"))) and any(m in u for m in _CDN_HOST_MARKERS)


def ig_thread_id(sender_psid: str) -> str:
    """Return a stable UUID string for Supabase ``chat_threads.id``."""
    return str(uuid.uuid5(IG_THREAD_NAMESPACE, f"instagram:{sender_psid}"))


def ig_memory_user_id(sender_psid: str) -> str:
    """Stable Mem0 / Hermes ``user_id`` string (not a Supabase UUID)."""
    return f"ig:{sender_psid}"


def fetch_instagram_user_profile(igsid: str) -> dict[str, str | None]:
    """Return ``username`` / ``name`` from Instagram Graph for a messaging sender id.

    Meta returns ``username`` when the account is eligible for profile lookup;
    failures are logged at debug level and an empty dict is returned.
    """
    raw_id = (igsid or "").strip()
    token = (os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN") or "").strip()
    if not raw_id or not token:
        return {}
    fields = "username,name"
    for base, label in ((_GRAPH_IG_BASE, "graph.instagram.com"), (_GRAPH_FB_BASE, "graph.facebook.com")):
        url = f"{base}/{raw_id}"
        try:
            with http_client_sync(timeout=8.0) as client:
                resp = client.get(
                    url,
                    params={"fields": fields, "access_token": token},
                )
        except Exception as exc:
            logger.debug("ig_profile: %s request failed: %s", label, exc)
            continue
        if resp.status_code != 200:
            logger.debug(
                "ig_profile: %s HTTP %s body=%s",
                label,
                resp.status_code,
                (resp.text or "")[:300],
            )
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        if isinstance(data, dict) and data.get("error"):
            logger.debug("ig_profile: %s error payload=%s", label, data.get("error"))
            continue
        u = data.get("username")
        n = data.get("name")
        out_u = str(u).strip() if isinstance(u, str) and u.strip() else None
        out_n = str(n).strip() if isinstance(n, str) and n.strip() else None
        if out_u or out_n:
            return {"username": out_u, "name": out_n}
    return {}


def plain_text_for_instagram_dm(text: str) -> str:
    """Strip markdown — Instagram DMs render plain text only (no **bold**, links ok)."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def split_reply(text: str, *, max_parts: int = 8, max_chars: int = 990) -> list[str]:
    """Split assistant text on ``[BREAK]`` for sequential IG sends."""
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(_BREAK) if p.strip()]
    out: list[str] = []
    for p in parts[:max_parts]:
        out.append(p[:max_chars])
    return out if out else [raw[:max_chars]]


def extract_urls(text: str) -> list[str]:
    """Return up to 5 http(s) URLs from a DM (Reels, TikTok, etc.)."""
    if not text:
        return []
    found = _URL_RE.findall(text)
    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        u = u.rstrip(").,;:]")
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= 5:
            break
    return out
