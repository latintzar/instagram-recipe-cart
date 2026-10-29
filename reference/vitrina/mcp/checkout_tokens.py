"""Checkout magic-link token management.

Each ``checkout.create_link`` call mints a row in ``mcp.checkout_tokens``.
The web page resolves the slug to basket IDs + slot, shows the basket
UI, collects auth / guest-email, and then calls ``POST /v1/magic/checkout/session``
which creates the Stripe embedded session and marks the token redeemed (via
the ``checkout.session.completed`` webhook).

Table schema (see migration 012_magic_link_tokens.sql):
    slug            TEXT PK
    cart_ids        TEXT[]
    cart_key        TEXT
    thread_id       TEXT
    slot_payload    JSONB
    expires_at      TIMESTAMPTZ
    redeemed_at     TIMESTAMPTZ
    created_at      TIMESTAMPTZ
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import supabase_client
from _magic_link import mint_unique_slug

_TABLE = "checkout_tokens"

# Magic checkout links: long enough for DM + distracted shoppers; still single-use after pay.
CHECKOUT_TOKEN_TTL = timedelta(hours=24)


def _exists(slug: str) -> bool:
    sb = supabase_client.get_supabase()
    rows = sb.table(_TABLE).select("slug").eq("slug", slug).limit(1).execute()
    return bool(rows)


def mint(
    *,
    cart_ids: list[str],
    cart_key: str,
    thread_id: str,
    slot: dict[str, Any] | None = None,
) -> str:
    """Insert a new checkout token and return its slug."""
    slug = mint_unique_slug(_exists)
    expires_at = (datetime.now(timezone.utc) + CHECKOUT_TOKEN_TTL).isoformat()
    supabase_client.get_supabase().table(_TABLE).insert(
        {
            "slug": slug,
            "cart_ids": cart_ids,
            "cart_key": cart_key,
            "thread_id": thread_id,
            "slot_payload": slot,
            "expires_at": expires_at,
        }
    ).execute()
    return slug


def lookup(slug: str) -> dict[str, Any] | None:
    """Return the token row if it is valid (not expired, not redeemed), else None."""
    sb = supabase_client.get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = (
        sb.table(_TABLE)
        .select("slug,cart_ids,cart_key,thread_id,slot_payload,expires_at,redeemed_at")
        .eq("slug", slug)
        .gt("expires_at", now)
        .is_("redeemed_at", None)
        .limit(1)
        .execute()
    )
    if not rows:
        return None
    row = rows[0] if isinstance(rows, list) else rows
    if isinstance(row, dict):
        return row
    return None


def mark_redeemed(slug: str) -> None:
    """Stamp ``redeemed_at`` so the slug cannot be replayed."""
    now = datetime.now(timezone.utc).isoformat()
    supabase_client.get_supabase().table(_TABLE).update(
        {"redeemed_at": now}
    ).eq("slug", slug).execute()
