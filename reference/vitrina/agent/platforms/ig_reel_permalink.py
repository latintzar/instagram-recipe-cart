"""Instagram Graph helpers: DM reel CDN → public ``instagram.com/reel/…`` URLs.

Used by :mod:`platforms.instagram_dm` webhooks and tested without importing Hermes.

Meta quirks:

- ``GET /{media-id}`` can return **100/33** for reels the Page token does not own.
- Message ``shares`` objects use **``link``**, not ``url``. Requesting
  ``fields=shares{url,…}`` can make Instagram Graph **omit** ``shares`` entirely.
  Use ``shares{link,url,type,…}`` and read both keys.
- ``link`` may point at ``lookaside.fbsbx.com/ig_messaging_cdn?asset_id=…``; we then
  ``GET /{asset_id}`` for ``permalink`` / ``shortcode`` like attachments do.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

from core import http_client_sync

logger = logging.getLogger(__name__)

GRAPH_FB_BASE = "https://graph.facebook.com/v23.0"
GRAPH_IG_BASE = "https://graph.instagram.com/v23.0"


def graph_media_base_for_token(token: str) -> str:
    """Host for ``GET /{{media-id}}`` — must match token flavour (IGAA vs EAAB)."""
    if token.startswith(("IGAA", "IGQ")):
        return GRAPH_IG_BASE
    return GRAPH_FB_BASE


def attachment_reel_video_id(payload_obj: dict[str, Any]) -> str | None:
    """Best-effort media id from a Messaging attachment payload."""
    if not isinstance(payload_obj, dict):
        return None
    for key in ("reel_video_id", "video_id", "media_id", "id"):
        val = payload_obj.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s.isdigit():
            return s
    return None


def resolve_ig_reel_video_id_to_permalink(reel_video_id: str) -> str | None:
    """``GET /{{id}}?fields=permalink,shortcode`` on the Graph media node."""
    token = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
    raw_id = (reel_video_id or "").strip()
    if not token or not raw_id:
        return None
    if not raw_id.isdigit():
        logger.info("ig_reel: reel_video_id not numeric, skip id=%r", raw_id)
        return None
    base = graph_media_base_for_token(token)
    url = f"{base}/{raw_id}"
    params = {"fields": "permalink,shortcode,media_type", "access_token": token}
    try:
        with http_client_sync(timeout=10.0) as client:
            resp = client.get(url, params=params)
        if resp.status_code >= 400:
            logger.warning(
                "ig_reel: permalink lookup HTTP %s body=%s media_id=%s",
                resp.status_code,
                resp.text[:400],
                raw_id,
            )
            return None
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning("ig_reel: permalink lookup error=%s", data.get("error"))
            return None
        perm = data.get("permalink")
        if isinstance(perm, str) and perm.startswith("http"):
            return perm.split("?", 1)[0].rstrip("/") + "/"
        sc = data.get("shortcode")
        if isinstance(sc, str) and sc.strip():
            return f"https://www.instagram.com/reel/{sc.strip()}/"
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig_reel: permalink resolve exception media_id=%s err=%s", raw_id, exc)
    return None


def graph_bases_for_message_lookup(token: str) -> list[str]:
    primary = graph_media_base_for_token(token)
    other = GRAPH_FB_BASE if primary == GRAPH_IG_BASE else GRAPH_IG_BASE
    return [primary] if primary == other else [primary, other]


def _normalize_public_instagram_path(url: str) -> str:
    u = url.split("?", 1)[0].rstrip("/") + "/"
    return u


def _asset_id_from_messaging_cdn_url(url: str) -> str | None:
    """Parse ``asset_id`` from ``lookaside.fbsbx.com/ig_messaging_cdn`` share links."""
    low = (url or "").lower()
    if "lookaside.fbsbx.com" not in low and "ig_messaging_cdn" not in low:
        return None
    q = parse_qs(urlparse(url).query)
    raw = (q.get("asset_id") or [None])[0]
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s.isdigit() else None


def public_instagram_url_from_message_graph_response(data: dict[str, Any]) -> str | None:
    """Extract a public ``instagram.com`` reel/post/tv URL from Message ``shares.data[]``.

    IG Messaging uses ``link`` on share rows (sometimes ``url``). CDN ``link`` values
    are resolved via the media node when possible.
    """
    shares = data.get("shares")
    if not isinstance(shares, dict):
        return None
    ig_candidates: list[str] = []
    cdn_asset_ids: list[str] = []
    for item in shares.get("data") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("url") or item.get("link")
        if not isinstance(raw, str) or not raw.startswith("http"):
            continue
        low = raw.lower()
        if ("instagram.com" in low or "l.instagram.com" in low) and (
            "/reel/" in low or "/p/" in low or "/tv/" in low
        ):
            ig_candidates.append(_normalize_public_instagram_path(raw))
            continue
        aid = _asset_id_from_messaging_cdn_url(raw)
        if aid:
            cdn_asset_ids.append(aid)
    for u in ig_candidates:
        return u
    for aid in cdn_asset_ids:
        resolved = resolve_ig_reel_video_id_to_permalink(aid)
        if resolved:
            return resolved
    return None


def resolve_ig_share_url_from_message_mid(mid: str) -> str | None:
    """``GET /{{message-id}}?fields=shares{{link,url,type,…}}`` using webhook ``mid``."""
    token = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
    raw_mid = (mid or "").strip()
    if not token or not raw_mid:
        return None
    # Must request ``link`` — IG uses ``link`` for shares; ``url`` alone drops the field.
    fields = "shares{link,url,type,id,name,description}"
    for base in graph_bases_for_message_lookup(token):
        api_url = f"{base}/{raw_mid}"
        params = {"fields": fields, "access_token": token}
        try:
            with http_client_sync(timeout=12.0) as client:
                resp = client.get(api_url, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ig_reel: message shares HTTP exception mid=%s base=%s err=%s",
                raw_mid,
                base,
                exc,
            )
            continue
        if resp.status_code >= 400:
            logger.info(
                "ig_reel: message shares lookup HTTP %s mid=%s base=%s body=%s",
                resp.status_code,
                raw_mid,
                base,
                resp.text[:350],
            )
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            logger.info(
                "ig_reel: message shares Graph error mid=%s base=%s err=%s",
                raw_mid,
                base,
                data.get("error"),
            )
            continue
        out = public_instagram_url_from_message_graph_response(data)
        if out:
            logger.info(
                "ig_reel: resolved mid=%s → public URL via Message.shares (base=%s)",
                raw_mid,
                base,
            )
            return out
    return None
