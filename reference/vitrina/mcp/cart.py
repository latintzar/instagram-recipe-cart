"""UCP shopping-cart handlers (backend of the ``dev.ucp.shopping.cart.*`` MCP tools).

Plain synchronous Python — the FastAPI routes in ``api/routes/carts.py``
wrap these in ``run_in_threadpool`` when called from an async context.
Each handler talks to Supabase (real REST via :mod:`supabase_client` or
the in-memory mock under ``tests/fixtures/supabase_mock.py`` during
tests).

Auth model
~~~~~~~~~~
Every cart row carries an ``(user_id, cart_key)`` pair — anonymous
carts have ``user_id = NULL`` and a client-generated ``cart_key``;
logged-in users have ``user_id`` and a ``NULL`` ``cart_key``. Handlers
that mutate or read a specific cart require the caller to prove
ownership by passing either ``user_id`` OR ``cart_key`` matching the
stored value; mismatches raise :class:`CartAccessDenied`. Callers can
merge anon carts into a user on login via :func:`transfer_cart`, which
also reassigns any ``chat_threads`` rows that carried the same key.

Pricing
~~~~~~~
Prices are always stored in minor units (cents for EUR — Bulgaria
adopted the euro on 2026-01-01 in this codebase's timeline). On
:func:`add_cart_item` we snapshot the product row into
``product_snapshot`` so later display stays stable even if the catalogue
moves, and we recompute ``subtotal_minor`` / ``total_minor`` from the
canonical ``price_minor`` column. Tax is a hard-coded zero today (VAT is
already baked into the EUR retail prices for Phase 1).

Transactionality caveat
~~~~~~~~~~~~~~~~~~~~~~~
Supabase's REST surface is single-table; :func:`transfer_cart` therefore
performs two sequential UPDATEs (carts, then chat_threads) rather than
one wrapped RPC. If the second fails the first is not rolled back. For
Phase 1 that's acceptable (the worst case is a logged-in user whose
threads didn't follow them — recoverable by re-running transfer). When
we need real atomicity we'll move this to a SECURITY DEFINER RPC.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import supabase_client
from _session_norm import normalize_session  # noqa: E402 — flat mcp/ dir

logger = logging.getLogger("vitrina.mcp.cart")

# Recipe ingredients reference products + shops that live in seed JSON
# (`mcp/seed/recipe_products.json`, `recipe_shops.json`) and may not yet
# be mirrored into Supabase. Looking those up via PostgREST therefore
# returns nothing and the cart insert fails with a FK violation. We fall
# back to the seed JSON so the recipe → basket flow works end-to-end
# even before the Supabase migration runs.
_SEED_DIR = Path(__file__).resolve().parent / "seed"


@lru_cache(maxsize=1)
def _seed_shops() -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("shops.json", "recipe_shops.json"):
        path = _SEED_DIR / name
        if not path.exists():
            continue
        try:
            rows.extend(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cart.seed_load_failed file=%s err=%s", name, exc)
    return {row["id"]: row for row in rows if row.get("id")}


_synced_shops: set[str] = set()


def _ensure_shop_row(shop_id: str) -> None:
    """Idempotently upsert a recipe shop row into Supabase.

    The `carts.shop_id REFERENCES shops(id)` FK means we cannot create a
    cart for a shop that's only in seed JSON. Rather than blocking on a
    migration (and to support future shops that arrive at runtime via
    chat extraction), we self-heal on first use.
    """

    if shop_id in _synced_shops:
        return
    seed = _seed_shops().get(shop_id)
    if not seed:
        return  # Real Supabase shops will already satisfy the FK.
    sb = _get_supabase()
    try:
        existing = sb.table("shops").select("id").eq("id", shop_id).execute() or []
        if existing:
            _synced_shops.add(shop_id)
            return
        address = seed.get("address") or {}
        sb.table("shops").insert({
            "id": shop_id,
            "name": seed.get("name_en") or seed.get("name_bg") or shop_id,
            "timezone": seed.get("timezone") or "Europe/Sofia",
            "opening_hours": seed.get("opening_hours") or {},
            "address": address.get("street_en") or address.get("street_bg") or address.get("city_en"),
            "lat": (seed.get("location") or {}).get("lat"),
            "lng": (seed.get("location") or {}).get("lng"),
            "image_url": seed.get("image"),
            "currency": seed.get("currency") or "EUR",
        }).execute()
        _synced_shops.add(shop_id)
        logger.info("cart.shop_backfilled id=%s", shop_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cart.shop_backfill_failed id=%s err=%s", shop_id, exc)


@lru_cache(maxsize=1)
def _seed_products() -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("products.json", "recipe_products.json"):
        path = _SEED_DIR / name
        if not path.exists():
            continue
        try:
            rows.extend(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cart.seed_load_failed file=%s err=%s", name, exc)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = row.get("id")
        if not pid:
            continue
        price_eur = float(row.get("price_eur") or 0)
        out[pid] = {
            "id": pid,
            "shop_id": row.get("shop_id"),
            "name": row.get("name_en") or row.get("name_bg") or pid,
            "description": row.get("description_en") or row.get("description_bg"),
            "category": row.get("category"),
            "unit": row.get("unit"),
            "price_eur": price_eur,
            "price_minor": int(round(price_eur * 100)),
            "image_url": row.get("image"),
        }
    return out


# ── exceptions ──────────────────────────────────────────────────────
class CartNotFound(Exception):
    pass


class CartAccessDenied(Exception):
    pass


class ProductNotFound(Exception):
    pass


# ── client indirection (tests monkeypatch this) ─────────────────────
def _get_supabase() -> Any:
    return supabase_client.get_supabase()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_owner(cart: dict, user_id: str | None, cart_key: str | None) -> None:
    if user_id and cart.get("user_id") == user_id:
        return
    if cart_key and cart.get("cart_key") == cart_key:
        return
    raise CartAccessDenied("cart_access_denied")


def _load_cart_row(cart_id: str) -> dict:
    sb = _get_supabase()
    row = sb.table("carts").select("*").eq("id", cart_id).single().execute()
    if not row:
        raise CartNotFound(cart_id)
    return row


def _load_line(cart_id: str, line_id: str) -> dict:
    sb = _get_supabase()
    rows = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .eq("id", line_id)
        .execute()
    )
    if not rows:
        raise CartNotFound(f"line:{line_id}")
    return rows[0]


def _cart_with_lines(cart_id: str) -> dict:
    sb = _get_supabase()
    cart = _load_cart_row(cart_id)
    lines = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .order("created_at")
        .execute()
    )
    cart = dict(cart)
    cart["line_items"] = lines or []
    cart["totals"] = _totals(cart["line_items"])
    return cart


def _totals(lines: list[dict]) -> dict:
    subtotal = sum(int(l.get("subtotal_minor") or 0) for l in lines)
    tax = sum(int(l.get("tax_minor") or 0) for l in lines)
    total = sum(int(l.get("total_minor") or 0) for l in lines)
    return {
        "subtotal_minor": subtotal,
        "tax_minor": tax,
        "total_minor": total,
        "item_count": sum(int(l.get("quantity") or 0) for l in lines),
    }


def _price_line(product: dict, quantity: int) -> dict:
    unit_minor = int(product.get("price_minor") or 0)
    base = unit_minor * max(0, int(quantity))
    return {
        "base_amount_minor": base,
        "discount_minor": 0,
        "subtotal_minor": base,
        "tax_minor": 0,
        "total_minor": base,
    }


def _lookup_product(product_id: str) -> dict:
    """Resolve a product row, falling back to seed JSON when needed.

    Recipe products (loaded from `seed/recipe_products.json`) aren't
    necessarily in the live Supabase `products` table yet. Falling back
    to the seed lets us insert cart line items with a denormalised
    `product_snapshot` even when there's no DB row to FK against.
    """

    sb = _get_supabase()
    try:
        row = sb.table("products").select("*").eq("id", product_id).single().execute()
    except Exception as exc:  # noqa: BLE001 — Supabase REST raises on missing row
        logger.warning("cart.product_lookup_failed id=%s err=%s", product_id, exc)
        row = None
    if row:
        return row
    seed = _seed_products().get(product_id)
    if seed:
        return seed
    raise ProductNotFound(product_id)


# ── public handlers ─────────────────────────────────────────────────
def create_cart(
    shop_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
    currency: str = "EUR",
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    if not user_id and not cart_key:
        raise ValueError("create_cart requires user_id or cart_key")
    _ensure_shop_row(shop_id)
    sb = _get_supabase()
    row = sb.table("carts").insert(
        {
            "shop_id": shop_id,
            "user_id": user_id,
            "cart_key": cart_key,
            "currency": currency,
            "status": "open",
        }
    ).single().execute()
    if isinstance(row, list):
        row = row[0] if row else None
    cart = dict(row or {})
    cart["line_items"] = []
    cart["totals"] = _totals([])
    return cart


def get_cart(
    cart_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    return _cart_with_lines(cart_id)


def list_user_carts(user_id: str) -> list[dict]:
    from _session_norm import coerce_uuid_or_none  # noqa: PLC0415
    user_id = coerce_uuid_or_none(user_id) or ""
    if not user_id:
        return []
    sb = _get_supabase()
    rows = (
        sb.table("carts")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "open")
        .order("updated_at", desc=True)
        .execute()
    )
    return rows or []


def list_carts_by_key(cart_key: str) -> list[dict]:
    sb = _get_supabase()
    rows = (
        sb.table("carts")
        .select("*")
        .eq("cart_key", cart_key)
        .eq("status", "open")
        .order("updated_at", desc=True)
        .execute()
    )
    return rows or []


def add_cart_item(
    cart_id: str,
    product_id: str,
    quantity: int,
    *,
    group_id: str | None = None,
    group_metadata: dict | None = None,
    replace: bool = False,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    if quantity <= 0:
        raise ValueError("quantity_must_be_positive")
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)

    product = _lookup_product(product_id)
    sb = _get_supabase()

    # Find an existing line for same (cart_id, product_id, group_id).
    existing = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .eq("product_id", product_id)
        .execute()
    ) or []
    existing = [l for l in existing if (l.get("group_id") or None) == (group_id or None)]

    if existing and not replace:
        line = existing[0]
        new_qty = int(line.get("quantity") or 0) + int(quantity)
        pricing = _price_line(product, new_qty)
        patch = {
            "quantity": new_qty,
            "product_snapshot": product,
            **pricing,
        }
        if group_metadata is not None:
            patch["group_metadata"] = group_metadata
        sb.table("cart_line_items").update(patch).eq("id", line["id"]).execute()
    else:
        if existing and replace:
            sb.table("cart_line_items").delete().eq("id", existing[0]["id"]).execute()
        pricing = _price_line(product, quantity)
        sb.table("cart_line_items").insert(
            {
                "cart_id": cart_id,
                "product_id": product_id,
                "quantity": int(quantity),
                "product_snapshot": product,
                "group_id": group_id,
                "group_metadata": group_metadata,
                **pricing,
            }
        ).execute()

    sb.table("carts").update({"updated_at": _now()}).eq("id", cart_id).execute()
    return _cart_with_lines(cart_id)


def update_cart_item(
    cart_id: str,
    line_id: str,
    quantity: int,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    line = _load_line(cart_id, line_id)
    sb = _get_supabase()

    if quantity <= 0:
        sb.table("cart_line_items").delete().eq("id", line_id).execute()
    else:
        product = line.get("product_snapshot") or {}
        pricing = _price_line(product, quantity)
        sb.table("cart_line_items").update(
            {"quantity": int(quantity), **pricing}
        ).eq("id", line_id).execute()

    sb.table("carts").update({"updated_at": _now()}).eq("id", cart_id).execute()
    return _cart_with_lines(cart_id)


def remove_cart_item(
    cart_id: str,
    line_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    _load_line(cart_id, line_id)
    sb = _get_supabase()
    sb.table("cart_line_items").delete().eq("id", line_id).execute()
    sb.table("carts").update({"updated_at": _now()}).eq("id", cart_id).execute()
    return _cart_with_lines(cart_id)


def remove_group_lines(
    cart_id: str,
    group_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    """Delete every line in ``cart_id`` with ``group_id`` (recipe teardown)."""
    user_id, cart_key = normalize_session(user_id, cart_key)
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    sb = _get_supabase()
    lines = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .eq("group_id", group_id)
        .execute()
    ) or []
    for line in lines:
        sb.table("cart_line_items").delete().eq("id", line["id"]).execute()
    sb.table("carts").update({"updated_at": _now()}).eq("id", cart_id).execute()
    return _cart_with_lines(cart_id)


def substitute_in_recipe(
    cart_id: str,
    group_id: str,
    original_product_id: str,
    replacement_product_id: str,
    reason: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    user_id, cart_key = normalize_session(user_id, cart_key)
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    sb = _get_supabase()

    lines = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .eq("group_id", group_id)
        .eq("product_id", original_product_id)
        .execute()
    ) or []
    if not lines:
        raise CartNotFound(f"group:{group_id}:{original_product_id}")
    line = lines[0]

    replacement = _lookup_product(replacement_product_id)
    quantity = int(line.get("quantity") or 0)
    pricing = _price_line(replacement, quantity)

    group_metadata = dict(line.get("group_metadata") or {})
    subs = list(group_metadata.get("substitutions") or [])
    subs.append(
        {
            "original_product_id": original_product_id,
            "replacement_product_id": replacement_product_id,
            "reason": reason,
            "at": _now(),
        }
    )
    group_metadata["substitutions"] = subs

    sb.table("cart_line_items").update(
        {
            "product_id": replacement_product_id,
            "product_snapshot": replacement,
            "group_metadata": group_metadata,
            **pricing,
        }
    ).eq("id", line["id"]).execute()
    sb.table("carts").update({"updated_at": _now()}).eq("id", cart_id).execute()
    return _cart_with_lines(cart_id)


def transfer_by_email(email: str, target_user_id: str) -> dict[str, Any]:
    """Reassign carts + threads where ``chat_threads.metadata.email`` matches.

    Intended for post-signup linking when an IG shopper attached an email via
    ``account.attach_email`` and later creates a web account with the same address.
    """
    email_n = (email or "").strip().lower()
    if not email_n or "@" not in email_n or not target_user_id:
        return {
            "ok": False,
            "error": "invalid_args",
            "carts_transferred": 0,
            "threads_transferred": 0,
        }
    sb = _get_supabase()
    rows = sb.table("chat_threads").select("id, cart_key, metadata").execute()
    keys: list[str] = []
    for row in rows or []:
        if not row:
            continue
        meta = row.get("metadata") or {}
        em = str(meta.get("email") or "").strip().lower()
        ck = row.get("cart_key")
        if em == email_n and ck:
            keys.append(str(ck))
    deduped = list(dict.fromkeys(keys))
    if not deduped:
        return {
            "ok": True,
            "carts_transferred": 0,
            "threads_transferred": 0,
            "message": "no_matching_threads",
        }
    out = transfer_cart(deduped, str(target_user_id))
    return {"ok": True, **out}


def transfer_cart(cart_keys: list[str], user_id: str) -> dict:
    """Reassign anonymous carts + chat_threads to a user on login.

    Two sequential PATCHes — not atomic (see module docstring). Returns
    the counts for telemetry/debugging.
    """
    if not cart_keys or not user_id:
        return {"carts_transferred": 0, "threads_transferred": 0}
    sb = _get_supabase()
    carts = (
        sb.table("carts")
        .update({"user_id": user_id, "cart_key": None})
        .in_("cart_key", cart_keys)
        .execute()
    )
    threads = (
        sb.table("chat_threads")
        .update({"user_id": user_id, "cart_key": None})
        .in_("cart_key", cart_keys)
        .execute()
    )
    return {
        "carts_transferred": len(carts or []),
        "threads_transferred": len(threads or []),
    }


def discard_cart(
    cart_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    cart = _load_cart_row(cart_id)
    _assert_owner(cart, user_id, cart_key)
    sb = _get_supabase()
    sb.table("carts").update(
        {"status": "discarded", "updated_at": _now()}
    ).eq("id", cart_id).execute()
    return {"cart_id": cart_id, "status": "discarded"}


def discard_current_carts(
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict:
    """Discard every open cart owned by the current shopper identity.

    The web UI only stores the *current* visible cart ids in localStorage,
    but during testing Peter can accumulate old open carts under the same
    anonymous cart_key. The chat agent's basket snapshot used to see those
    old carts even when the basket sheet did not. Clearing the basket should
    therefore be an owner-wide operation: wipe all open carts for the same
    user/cart_key and then let the UI forget its local ids.
    """
    user_id, cart_key = normalize_session(user_id, cart_key)
    if not user_id and not cart_key:
        raise ValueError("discard_current_carts requires user_id or cart_key")

    rows = list_user_carts(user_id) if user_id else list_carts_by_key(cart_key or "")
    sb = _get_supabase()
    ids = [row["id"] for row in rows if row.get("id")]
    if ids:
        sb.table("carts").update(
            {"status": "discarded", "updated_at": _now()}
        ).in_("id", ids).execute()
    return {"status": "discarded", "cart_ids": ids, "count": len(ids)}
