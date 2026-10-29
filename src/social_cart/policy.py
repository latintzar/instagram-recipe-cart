"""Validate untrusted reel links before the extraction process."""
import re
from urllib.parse import parse_qs, urlsplit

REEL_PATH = re.compile(r"^/(?:reel|reels|p|tv)/[A-Za-z0-9_-]{3,64}/?$")

def reel_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid reel URL")
    try:
        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443) or u.fragment:
            raise ValueError("invalid reel URL")
        host = u.hostname.lower()
        if host in {"instagram.com", "www.instagram.com"} and REEL_PATH.fullmatch(u.path):
            return f"https://www.instagram.com{u.path.rstrip('/')}/"
        if host == "lookaside.fbsbx.com" and u.path.rstrip("/") == "/ig_messaging_cdn":
            asset = parse_qs(u.query).get("asset_id", [])
            if len(asset) == 1 and asset[0].isdigit():
                return value
    except (TypeError, ValueError):
        raise ValueError("invalid reel URL") from None
    raise ValueError("unsupported reel URL")

def https_origin(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError
        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ValueError
        if u.port not in (None, 443):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise ValueError("an HTTPS origin is required") from None
