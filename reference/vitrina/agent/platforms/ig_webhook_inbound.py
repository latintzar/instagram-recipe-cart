"""Normalise Meta Instagram Messaging webhook payloads into agent text snippets.

Kept separate from :mod:`platforms.instagram_dm` so pytest can import it without
Hermes / FastAPI.

Voice notes: attachments with ``type`` ``audio`` yield ``voice_attachment_urls``
(HTTPS CDN URLs). :func:`platforms.instagram_dm.handle_webhook` downloads those
bytes and runs the same ``voice.transcribe`` stack as ``POST /api/transcribe``
before Hermes sees the message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

try:
    from .ig_reel_permalink import (
        attachment_reel_video_id,
        resolve_ig_reel_video_id_to_permalink,
        resolve_ig_share_url_from_message_mid,
    )
    from .instagram_platform import has_instagram_permalink_in_text, is_instagram_private_media_host
except ImportError:  # pragma: no cover
    from platforms.ig_reel_permalink import (  # type: ignore
        attachment_reel_video_id,
        resolve_ig_reel_video_id_to_permalink,
        resolve_ig_share_url_from_message_mid,
    )
    from platforms.instagram_platform import (  # type: ignore
        has_instagram_permalink_in_text,
        is_instagram_private_media_host,
    )

logger = logging.getLogger(__name__)


def iter_instagram_messaging_events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield normalised message dicts from a Meta webhook payload."""
    for entry in payload.get("entry", []) or []:
        for ev in entry.get("messaging", []) or []:
            sender = (ev.get("sender") or {}).get("id")
            msg = ev.get("message") or {}
            mid = msg.get("mid")
            is_echo = bool(msg.get("is_echo"))
            text_part = (msg.get("text") or "").strip()
            qr = msg.get("quick_reply")
            has_quick_reply = isinstance(qr, dict) and qr.get("payload") is not None

            attachment_urls: list[str] = []
            attachment_kinds: list[str] = []
            voice_attachment_urls: list[str] = []
            resolved_permalink: str | None = None
            for att in msg.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                kind = str(att.get("type") or "")
                payload_obj = att.get("payload") if isinstance(att.get("payload"), dict) else {}
                url = payload_obj.get("url") if isinstance(payload_obj, dict) else None
                kl = kind.lower()
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    if kl in ("audio", "ig_audio"):
                        voice_attachment_urls.append(url)
                    else:
                        attachment_urls.append(url)
                        attachment_kinds.append(kind or "share")
                if kind in ("ig_reel", "reel") and resolved_permalink is None:
                    rid = attachment_reel_video_id(payload_obj)
                    if rid:
                        resolved_permalink = resolve_ig_reel_video_id_to_permalink(rid)
                        if resolved_permalink:
                            logger.info(
                                "ig_webhook: resolved reel_video_id=%s → permalink",
                                rid,
                            )

            # Skip shares lookup for echo messages (bot's own outgoing messages).
            # The Graph API rejects /shares lookups on outgoing mids with
            # "Invalid OAuth access token" because the IGAA token cannot
            # inspect its own sent messages' share metadata. Echoes never
            # contain user-shared reels anyway.
            if resolved_permalink is None and mid and not is_echo:
                resolved_permalink = resolve_ig_share_url_from_message_mid(str(mid))

            if not sender:
                continue
            if (
                not text_part
                and not attachment_urls
                and not resolved_permalink
                and not has_quick_reply
                and not voice_attachment_urls
            ):
                continue

            permalink_in_text = has_instagram_permalink_in_text(text_part)
            # Meta IG Messaging CDN (``lookaside.fbsbx.com``) URLs serve raw
            # MP4 directly — recipe_videos.extract_social_recipe handles them
            # via direct httpx download.  Treat them as extractable, NOT as
            # ``reel_cdn_only`` (which previously told the agent to give up).
            non_lookaside_cdn_only = bool(attachment_urls) and all(
                is_instagram_private_media_host(u) and "lookaside.fbsbx.com" not in u.lower()
                for u in attachment_urls
            )
            reel_cdn_only = (
                (not resolved_permalink)
                and non_lookaside_cdn_only
                and (not permalink_in_text)
            )

            text = text_part
            if attachment_urls or resolved_permalink:
                kinds = (
                    ", ".join(sorted(set(attachment_kinds)))
                    or ("ig_reel" if resolved_permalink else "share")
                )
                if resolved_permalink:
                    appendix = (
                        f"[Shared {kinds}; resolved public URL for extraction: "
                        f"{resolved_permalink}]"
                    )
                else:
                    appendix = f"[Shared {kinds}: {', '.join(attachment_urls)}]"
                text = f"{text}\n\n{appendix}".strip() if text else appendix

            if has_quick_reply:
                pl = qr.get("payload")
                pl_s = pl if isinstance(pl, str) else json.dumps(pl, ensure_ascii=False)
                qr_block = (
                    f"\n\n[Instagram quick_reply: payload={pl_s!r}; "
                    f"button_or_display_text={text_part!r}. "
                    "Treat payload as authoritative intent: check it against the "
                    "conversation goal, clarify in one sentence if it conflicts or "
                    "is stale, then continue — do not perform engagement for a tap "
                    "you know is wrong.]"
                )
                text = f"{text}{qr_block}".strip()

            yield {
                "sender_id": str(sender),
                "text": text,
                "mid": mid,
                "is_echo": is_echo,
                "reel_cdn_only": reel_cdn_only,
                "voice_attachment_urls": voice_attachment_urls,
            }
