"""Server-side session-argument normalisation for Vitrina MCP tools.

The model sometimes passes malformed infrastructure arguments:
- ``user_id="ig:967387775706018"`` — a raw IG PSID instead of a UUID.
- ``user_id="967387775706018"`` — same PSID without the prefix.
- ``thread_id="00000000-0000-0000-0000-000000000000"`` — all-zeros sentinel.

All of these hit Postgres UUID columns and produce ``22P02`` (invalid uuid
syntax) or ``23503`` (foreign-key violation). The fix is at the server
boundary: any non-UUID-shaped value for a UUID column is coerced to None
before it reaches the DB layer.

For anonymous-channel shoppers (cart_key starting with ``ig:``, ``tt:``,
``vb:``, ``wa:``) there is no Supabase user row, so user_id must always be
None regardless of what the model passes.

These helpers are called at the top of every cart handler and the recipe
extraction async entry-point so the validation is universal and testable
in isolation.
"""

from __future__ import annotations

import re
from typing import Any

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Channel prefixes that mark anonymous sessions with no Supabase user row.
_ANON_PREFIXES = ("ig:", "tt:", "vb:", "wa:", "tk:")


def coerce_uuid_or_none(value: Any) -> str | None:
    """Return *value* if it's a well-formed UUID string, otherwise None.

    Silently drops IG PSIDs, bare integers, sentinel all-zeros UUIDs, and
    any other non-UUID-shaped strings so they never reach Postgres.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if _UUID_RE.match(s):
        # Reject the sentinel all-zeros UUID — it's never a real row.
        if s.replace("-", "") == "0" * 32:
            return None
        return s
    return None


def normalize_session(
    user_id: Any,
    cart_key: Any,
) -> tuple[str | None, str | None]:
    """Return a normalised *(user_id, cart_key)* pair.

    Rules:
    1. ``user_id`` must be a valid UUID or None.  Non-UUID values are
       dropped (see :func:`coerce_uuid_or_none`).
    2. When ``cart_key`` signals an anonymous-channel session
       (``ig:``, ``tt:``, ``vb:``, ``wa:`` prefix) ``user_id`` is forced
       to None — there is no corresponding Supabase Auth user.
    3. ``cart_key`` is returned as-is (it's a free-form opaque string,
       not a UUID).
    """
    cart_key_str: str | None = str(cart_key).strip() if cart_key else None
    is_anon = bool(
        cart_key_str and any(cart_key_str.startswith(p) for p in _ANON_PREFIXES)
    )

    if is_anon:
        return None, cart_key_str

    return coerce_uuid_or_none(user_id), cart_key_str or None
