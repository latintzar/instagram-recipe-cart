"""LLM-based recipe → shops matcher.

This is THE core engine of Vitrina: turning a free-text recipe (extracted
from a social video) into a concrete, *buyable* sourcing plan across the
indie shops in the user's catchment.

Why an LLM (fast multimodal model by default)
---------------------------------------------

Every indie shop ships its own SKU vocabulary — there is no shared
ontology between "Кашкавал „Балкан\" пушен 500г" and "yellow cheese,
smoked, 500g". Recipes use loose names too ("rock sugar", "good olive
oil"). Hard rules break instantly. Sonnet reads the ingredient + product
description in context, decides "this product covers this ingredient
faithfully" *or* "this is an acceptable substitute, here's why", flags
genuinely missing items, and proposes a per-shop sourcing plan that
minimises shop-hopping.

Two-stage matching
------------------

Stage A — **universal pre-bake** (`match_recipe(..., catchment_km=None)`)
  Run once at extraction time against the full Sofia shop universe.
  Persisted on the catalog row as ``match_universal``. Cheap to consult
  (~4-6k input tokens, ~700 output tokens, ≈ $0.03). Tells us whether the
  recipe is *theoretically* buyable in our market at all.

Stage B — **per-user resolve** (`resolve_for_user(...)`)
  Take the universal plan, drop products whose shop is outside the
  user's catchment radius. If the recipe slips from `full` → `partial`
  the matcher re-runs with the in-catchment shop list to look for
  alternatives. Cached (process-local LRU keyed on the recipe id +
  the sorted in-catchment shop ids) so the second LLM call only fires
  on novel catchment shapes.

Catchment policy (constitution §15: never descope distance)
- Default radius: 1.5 km (~18 min walk @ 4.5 km/h × 1.3 detour).
- Auto-expanded to 2.5 km when the 1.5 km result comes back partial AND
  candidate substitutes exist in the 1.5–2.5 km ring.
- A recipe with non-empty ``blockers`` (something genuinely unbuyable)
  is hidden from Discover but still openable by direct id, so a user
  who tapped from chat sees a helpful "missing X — but rest is buyable"
  message instead of a 404.

The matcher is intentionally a thin module — no FastAPI, no Supabase,
just I/O via OpenRouter and pure-data inputs. That makes it trivial
to (a) run from a CLI backfill, (b) test offline, (c) swap models
later (Haiku for ranking, GPT-4o for fallback) without rewriting
callers.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    from .unit_parser import parse_pack_grams
except ImportError:  # script-style execution
    from unit_parser import parse_pack_grams  # type: ignore

logger = logging.getLogger("vitrina.recipe_videos.shop_matcher")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Default model: anthropic/claude-haiku-4.5 via OpenRouter. Switched from
# google/gemini-2.5-flash on 2026-04 — Haiku 4.5 is faster on Vitrina's
# structured-output prompt (recipe → per-shop sourcing JSON), holds the
# JSON schema more reliably under load, and costs roughly the same per
# match. Override via the env var if you're A/B-testing a swap.
DEFAULT_MODEL = os.environ.get("VITRINA_MATCHER_MODEL", "anthropic/claude-haiku-4.5")

DEFAULT_CATCHMENT_KM = 1.5
EXPANDED_CATCHMENT_KM = 2.5
WALKING_KMH = 4.5
DETOUR_FACTOR = 1.3
EARTH_R_KM = 6371.0088

HERE = Path(__file__).resolve().parent
SEED_DIR = HERE.parent / "mcp" / "seed"


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _all_shops_indexed() -> dict[str, dict[str, Any]]:
    rows = json.loads((SEED_DIR / "shops.json").read_text(encoding="utf-8"))
    extra = SEED_DIR / "recipe_shops.json"
    if extra.exists():
        rows.extend(json.loads(extra.read_text(encoding="utf-8")))
    return {row["id"]: row for row in rows}


@lru_cache(maxsize=1)
def _all_products_grouped() -> dict[str, list[dict[str, Any]]]:
    rows = json.loads((SEED_DIR / "products.json").read_text(encoding="utf-8"))
    extra = SEED_DIR / "recipe_products.json"
    if extra.exists():
        rows.extend(json.loads(extra.read_text(encoding="utf-8")))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for product in rows:
        grouped.setdefault(product["shop_id"], []).append(product)
    return grouped


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in kilometres — used for catchment filtering."""
    d_lat = math.radians(b_lat - a_lat)
    d_lng = math.radians(b_lng - a_lng)
    sin_lat = math.sin(d_lat / 2)
    sin_lng = math.sin(d_lng / 2)
    h = sin_lat * sin_lat + math.cos(math.radians(a_lat)) * math.cos(
        math.radians(b_lat)
    ) * sin_lng * sin_lng
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(h)))


def walking_minutes(km: float) -> int:
    """Human-friendly walking-time approximation for the modal pill."""
    minutes = (km * DETOUR_FACTOR) / WALKING_KMH * 60
    return max(1, int(round(minutes)))


def shop_with_distance(
    shop: dict[str, Any], user_lat: float | None, user_lng: float | None
) -> dict[str, Any]:
    """Decorate a shop with ``distance_km`` + ``walking_minutes`` if a user pin is known."""
    address = shop.get("address") or {}
    out = {
        "id": shop["id"],
        "name": shop.get("name_en") or shop.get("name_bg") or shop["id"],
        "trade": shop.get("trade_en") or shop.get("trade"),
        "address": address.get("street_en")
        or address.get("street_bg")
        or address.get("city_en"),
        "lat": address.get("lat"),
        "lng": address.get("lng"),
    }
    if user_lat is not None and user_lng is not None and out["lat"] and out["lng"]:
        km = haversine_km(user_lat, user_lng, out["lat"], out["lng"])
        out["distance_km"] = round(km, 3)
        out["walking_minutes"] = walking_minutes(km)
    return out


def _compact_product(product: dict[str, Any]) -> dict[str, Any]:
    """Trim a product row to just what the matcher LLM needs to reason about.

    The matcher's recall depends on this being information-dense: keywords
    + subcategory + cuisine give the LLM the synonyms / hierarchy it needs
    to map "fresh basil" → ``ZP-BASIL-30`` even when the recipe text and
    the SKU name don't share a literal token. See ``seed/ONTOLOGY.md``.
    """
    return {
        "id": product["id"],
        "name": product.get("name_en") or product.get("name_bg") or product["id"],
        "description": product.get("description_en") or product.get("description_bg"),
        "category": product.get("category"),
        "subcategory": product.get("subcategory"),
        "cuisine": product.get("cuisine"),
        "keywords": product.get("keywords") or [],
        "unit": product.get("unit"),
        "unit_size": product.get("unit_size"),
        "price_eur": float(product.get("price_eur") or 0),
        "stock": product.get("stock"),
    }


def _compact_shop_with_inventory(
    shop: dict[str, Any], products: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Shape a shop + its inventory into the schema the matcher prompt expects."""
    address = shop.get("address") or {}
    return {
        "id": shop["id"],
        "name": shop.get("name_en") or shop.get("name_bg") or shop["id"],
        "trade": shop.get("trade_en") or shop.get("trade"),
        "tagline": shop.get("tagline_en") or shop.get("tagline_bg"),
        "lat": address.get("lat"),
        "lng": address.get("lng"),
        "products": [_compact_product(p) for p in products],
    }


def shops_in_catchment(
    user_lat: float,
    user_lng: float,
    *,
    radius_km: float = DEFAULT_CATCHMENT_KM,
    universe: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return every shop whose Haversine distance to the user pin is within radius."""
    universe = universe or _all_shops_indexed()
    result: list[dict[str, Any]] = []
    for shop in universe.values():
        address = shop.get("address") or {}
        lat = address.get("lat")
        lng = address.get("lng")
        if lat is None or lng is None:
            continue
        km = haversine_km(user_lat, user_lng, lat, lng)
        if km <= radius_km:
            result.append(shop)
    return result


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You are Vitrina's buyer agent. Vitrina lets people watch a recipe video and immediately buy every ingredient from indie neighbourhood shops.

Each product in the supplied universe carries:
  - ``category`` — one of 10 broad buckets: produce, bakery, dairy, cheese,
    meat, pantry, canned, condiment, beverage, specialty (plus shop-trade
    buckets like ``hardware`` for non-grocery shops). Use this to narrow
    the search space — a recipe asking for "olive oil" should only ever
    map to ``pantry`` SKUs, never to ``condiment`` or ``produce``.
  - ``subcategory`` — finer hint inside the bucket (``leafy``, ``yellow``
    cheese, ``noodles``, ``hot-sauce``, …).
  - ``cuisine`` — optional tags like ``["asian"]`` or ``["mediterranean"]``.
    Treat as a soft prior: a Korean recipe should weight Asian-tagged
    products higher when there are ties.
  - ``keywords`` — synonym list in EN + BG (Cyrillic). Match aggressively
    against this. "Cabbage" should match a SKU whose keywords include
    ``["cabbage", "зеле", "white cabbage", "savoy", "napa"]``.

Ingredient-text normalisation: before matching, strip leading quantities
("2 cups", "500 g", "a handful of") and qualifiers ("fresh", "free-range",
"organic") to recover the canonical noun. "Fresh basil leaves" → match on
"basil"; "1 cup all-purpose flour" → match on "flour" / "plain flour".

When the recipe row carries an explicit ``canonical`` field (the
extractor's pre-normalised noun, e.g. "garlic" / "yellow onion"), use
that as the primary search key instead of re-deriving from
``description``. The ``prep`` field ("minced", "thinly sliced", "grated")
is for the shopper's reading pleasure only — never let it influence
which SKU you pick.

**Pack-size reasoning is mandatory.** Real shops sell in fixed packs
(500 g flour, 250 ml cream, 6-pack eggs) and recipe needs almost never
match exactly. You MUST reason about the gap:

  * If ``quantity_g`` (recipe need) ≤ pack grams → buy 1 pack and
    note the leftover, e.g. ``quantity_purchase: "1 pack (250 g — you'll
    have ~50 g left over)"``.
  * If ``quantity_g`` > pack grams → round UP to the next whole pack,
    e.g. need 800 g sugar at 500 g/pack → ``quantity_purchase: "2 packs
    (1 kg — slightly more than the 800 g needed)"``.
  * For piece items (eggs, lemons, garlic heads), translate need → packs
    of the available pack size. "6 eggs" with a 10-pack SKU →
    ``quantity_purchase: "1 carton of 10"``.
  * Never refuse to buy because the pack doesn't match exactly. Pack
    mismatch is normal and shoppers expect it; just be transparent.

When you write the ``substitution`` note, mention the pack reality if
it's unusual (e.g. "Sold in 1 kg sacks; 2 sacks cover the 1.4 kg need
and you'll have 600 g for next week's bread.").

Your job for a single recipe:
  1. Decide whether the recipe is buyable from the supplied shop universe.
  2. For each ingredient, pick the best matching product across all shops.
     - First try exact / near-exact matches.
     - **Substitute aggressively** — Vitrina is a sourcing tool, not a
       purist. Real cooks swap ingredients all the time. Examples of
       acceptable substitutions you SHOULD make rather than declaring
       missing:
         * "fresh basil/parsley/cilantro/mint" → any fresh herb in stock,
           OR a dried equivalent at a green grocer / spice shop.
         * "fresh herbs" / "mixed herbs" → parsley, dill, cilantro, basil,
           mint, or a close leafy herb. Never mark generic herbs missing
           while any herb SKU exists.
         * "coleslaw" / "slaw" → cabbage (white, red, savoy/napa) plus
           carrot/mayo if the recipe specifically asks for a finished slaw.
         * "guajillo / ancho / pasilla peppers" → any other dried chile.
         * "scallions / spring onions" → leek tops, chives, or shallots.
         * "buttermilk" → yoghurt thinned with milk, or kefir.
         * "self-raising flour" → plain flour + baking powder.
         * "Burger King Whopper / a McDonald's Big Mac" → ground beef +
           bun + cheese + pickles + onion (parts list — split across
           shops if needed, never declare missing).
         * Brand-name SKUs ("Heinz ketchup", "Lurpak butter") → any
           ketchup / butter on offer.
       Only mark something missing/blocker when NO plausible swap exists
       in the supplied universe. When in doubt, substitute and use the
       substitution string to be transparent about the swap.
     - Do not invent products. Only choose from the supplied product ids.
     - Output customer-facing copy only: no SKU ids in parentheses, no
       "customer should confirm", no internal matcher notes. If you are
       substituting, write a short friendly culinary note like
       "Fresh parsley works here in place of cilantro."
     - When two shops both stock the right product, prefer the closer one
       (smaller distance_km).
  3. Group selected products by shop, minimising shop-hopping.
  4. Be honest about gaps. If an ingredient is not represented in the
     universe AND has no acceptable substitute, list it as a blocker
     (the recipe cannot be cooked) or as a non-blocking missing item
     (the recipe still works, e.g. a garnish).
  5. **Quantities are mandatory.** NEVER output the literal string
     "unspecified" or leave ``quantity_recipe`` empty/null. If the recipe
     does not give a quantity, infer a reasonable default based on the
     dish, the servings count, and culinary common sense — e.g.
       * "to taste" for salt, pepper, herbs, spices.
       * "1 pack" / "1 jar" for everyday staples.
       * Numeric estimates ("250 g", "2 tbsp", "1 clove") when the rest
         of the recipe makes the proportion implicit.
     The number of *purchase units* (``quantity_purchase``) must always
     be concrete: "1 pack", "2 jars", "300 g", etc.
  6. **Always emit ``quantity_g``** (best-effort grams of recipe need)
     alongside ``quantity_recipe``. Convert pounds, ounces, cups, tbsp,
     tsp, ml, l to grams using culinary common sense (e.g. 1 cup flour
     ≈ 125 g, 1 cup water ≈ 240 g, 1 tbsp oil ≈ 14 g, 1 lb ≈ 454 g).
     For piece-only items where grams don't apply (e.g. "2 star anise",
     "1 lemon"), set ``quantity_g`` to null. The leftover-driven
     stretch logic depends on this number — getting it roughly right
     is more important than perfect precision.

Always respond with strict JSON matching the schema in the user message.
Do not chat. Do not include markdown fences. Confidence values must be
between 0 and 1."""


OUTPUT_SCHEMA_HINT = """{
  "feasibility": "full" | "partial" | "infeasible",
  "summary": "1-sentence human-friendly verdict (≤140 chars)",
  "shop_groups": [
    {
      "shop_id": "string",
      "items": [
        {
          "ingredient": "exact ingredient text from the recipe",
          "quantity_recipe": "string, e.g. '1.5 lb' or '2 cloves'",
          "quantity_purchase": "what to actually buy, e.g. '1 pack', '2 jars'",
          "quantity_g": "number or null — recipe need converted to grams",
          "product_id": "string from the supplied universe",
          "substitution": "string or null — non-null when the product is not exact",
          "confidence": 0.0
        }
      ]
    }
  ],
  "missing": [
    { "ingredient": "string", "blocking": false, "suggestion": "string" }
  ],
  "blockers": [
    { "ingredient": "string", "blocking": true, "suggestion": "string or null" }
  ]
}"""


def _build_user_prompt(
    recipe: dict[str, Any],
    shops_payload: list[dict[str, Any]],
    *,
    catchment_km: float | None,
    user_pin: tuple[float, float] | None,
) -> str:
    catchment_line = (
        f"Catchment radius (hard limit): {catchment_km} km from the user pin."
        if catchment_km is not None
        else "Catchment: unbounded (universal pre-bake — assume distance is acceptable)."
    )
    pin_line = (
        f"User pin: lat={user_pin[0]:.5f}, lng={user_pin[1]:.5f}."
        if user_pin
        else "User pin: not supplied (universal pre-bake)."
    )
    return f"""RECIPE
{json.dumps(_compact_recipe(recipe), ensure_ascii=False, indent=2)}

SHOP UNIVERSE
{pin_line}
{catchment_line}
{json.dumps(shops_payload, ensure_ascii=False, indent=2)}

OUTPUT — strict JSON only. Schema:
{OUTPUT_SCHEMA_HINT}
"""


def _compact_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    ingredients = []
    for item in recipe.get("ingredients") or []:
        ingredients.append(
            {
                "description": item.get("description") or "",
                # The matcher prefers the extractor's pre-normalised
                # canonical noun (single unbranded singular lowercase
                # English) when present — this is the field shoppers
                # said the matcher should reason from. Falls back to
                # description for legacy / synthesised recipes.
                "canonical": item.get("canonical") or "",
                "prep": item.get("prep") or "",
                "quantity": item.get("quantity"),
                "units": item.get("units"),
                # Recipe need converted to grams via culinary common
                # sense — required for the pack-vs-need reasoning step.
                "quantity_g": item.get("quantity_g"),
                "optional": bool(item.get("optional", False)),
                "evidence": item.get("evidence"),
            }
        )
    return {
        "title": recipe.get("title"),
        "description": recipe.get("description"),
        "servings": (recipe.get("servings") or {}).get("quantity"),
        "ingredients": ingredients,
    }


def _call_openrouter(
    user_prompt: str,
    *,
    model: str,
    api_key: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": model,
        "temperature": 0.1,
        # Sonnet's effective max-tokens is much higher than we need;
        # OpenRouter pre-charges credits against this ceiling so leaving
        # it unset gobbled $4 per call. 4096 comfortably fits even the
        # 6-shop recipes (largest seen was ~1900 output tokens).
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://example.invalid",
            "X-Title": "Vitrina Recipe Shop Matcher",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter request failed ({exc.code}): {body}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = data["choices"][0]["message"]["content"]
    text = text.strip()
    if text.startswith("```"):
        # Defensive — Sonnet sometimes wraps despite response_format=json_object.
        text = text.lstrip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    parsed = json.loads(text)
    usage = data.get("usage") or {}
    meta = {
        "model": model,
        "ms": elapsed_ms,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    return parsed, meta


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _hydrate_match(
    raw: dict[str, Any],
    *,
    shops_index: dict[str, dict[str, Any]],
    products_index: dict[str, dict[str, Any]],
    user_lat: float | None,
    user_lng: float | None,
    catchment_km: float | None,
    expanded_to_km: float | None,
    model_meta: dict[str, Any],
) -> dict[str, Any]:
    """Take Sonnet's bare ids + descriptions and inflate into the public match payload."""
    feasibility = raw.get("feasibility") or "infeasible"
    blockers = raw.get("blockers") or []
    if blockers:
        feasibility = "infeasible"

    shop_groups: list[dict[str, Any]] = []
    total_minor = 0
    for group in raw.get("shop_groups") or []:
        shop_id = group.get("shop_id")
        if not shop_id:
            continue
        shop_row = shops_index.get(shop_id)
        if not shop_row:
            logger.warning("matcher.unknown_shop_id id=%s", shop_id)
            continue
        items: list[dict[str, Any]] = []
        subtotal_minor = 0
        for item in group.get("items") or []:
            product_id = item.get("product_id")
            if not product_id:
                continue
            product_row = products_index.get(product_id)
            if not product_row or str(product_row.get("shop_id")) != str(shop_id):
                logger.warning("matcher.unknown_product_id id=%s", product_id)
                continue
            price_eur = float(product_row.get("price_eur") or 0)
            price_minor = int(round(price_eur * 100))
            subtotal_minor += price_minor
            pack = parse_pack_grams(
                product_row.get("unit"), product_row.get("unit_size")
            )
            qty_g_raw = item.get("quantity_g")
            try:
                qty_g = float(qty_g_raw) if qty_g_raw is not None else None
            except (TypeError, ValueError):
                qty_g = None
            items.append(
                {
                    "ingredient": item.get("ingredient") or "",
                    "quantity_recipe": item.get("quantity_recipe"),
                    "quantity_purchase": item.get("quantity_purchase"),
                    "quantity_g": qty_g,
                    "pack_grams": pack.grams,
                    "product": {
                        "id": product_row["id"],
                        "shop_id": shop_id,
                        "name": product_row.get("name_en")
                        or product_row.get("name_bg")
                        or product_row["id"],
                        "description": product_row.get("description_en")
                        or product_row.get("description_bg"),
                        "category": product_row.get("category"),
                        "unit": product_row.get("unit"),
                        "unit_size": product_row.get("unit_size"),
                        "price_eur": price_eur,
                        "price_minor": price_minor,
                        "image_url": product_row.get("image"),
                    },
                    "substitution": item.get("substitution"),
                    "confidence": float(item.get("confidence") or 0.0),
                }
            )
        if not items:
            continue
        total_minor += subtotal_minor
        shop_groups.append(
            {
                "shop": shop_with_distance(shop_row, user_lat, user_lng),
                "items": items,
                "subtotal_eur": round(subtotal_minor / 100, 2),
                "subtotal_minor": subtotal_minor,
            }
        )

    return {
        "feasibility": feasibility,
        "summary": (raw.get("summary") or "").strip(),
        "shop_groups": shop_groups,
        "missing": [
            {
                "ingredient": item.get("ingredient") or "",
                "blocking": False,
                "suggestion": item.get("suggestion"),
            }
            for item in raw.get("missing") or []
        ],
        "blockers": [
            {
                "ingredient": item.get("ingredient") or "",
                "blocking": True,
                "suggestion": item.get("suggestion"),
            }
            for item in blockers
        ],
        "total_eur": round(total_minor / 100, 2),
        "total_minor": total_minor,
        "currency": "EUR",
        "catchment_km": catchment_km,
        "expanded_to_km": expanded_to_km,
        "user_pin": {"lat": user_lat, "lng": user_lng}
        if user_lat is not None and user_lng is not None
        else None,
        "model_meta": model_meta,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def match_recipe(
    recipe: dict[str, Any],
    *,
    user_lat: float | None = None,
    user_lng: float | None = None,
    catchment_km: float | None = DEFAULT_CATCHMENT_KM,
    shops: list[dict[str, Any]] | None = None,
    products: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
    auto_expand: bool = True,
) -> dict[str, Any]:
    """Match a recipe against the indie shop universe.

    - ``user_lat`` / ``user_lng`` + ``catchment_km`` enforce a hard distance
      cap. Pass ``catchment_km=None`` for the *universal pre-bake* (every
      shop, no distance filter).
    - When ``auto_expand`` is true and the result is ``partial``, the
      matcher retries once with the expanded ring (2.5 km) before
      returning. The final result records both ``catchment_km`` and
      (if widened) ``expanded_to_km`` so the UI can mention "we looked a
      bit further" honestly.
    - ``shops`` overrides the universe (used by tests + per-user resolve
      paths that pre-filter by catchment).
    """

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for shop matching")

    if shops is None or products is None:
        raise ValueError("shops and products from your grocery service are required")
    shop_universe_index = {str(shop["id"]): shop for shop in shops}
    product_universe_grouped: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        product_universe_grouped.setdefault(str(product["shop_id"]), []).append(product)

    if shops is None:
        if (
            catchment_km is not None
            and user_lat is not None
            and user_lng is not None
        ):
            shops = shops_in_catchment(
                user_lat,
                user_lng,
                radius_km=catchment_km,
                universe=shop_universe_index,
            )
        else:
            shops = list(shop_universe_index.values())

    if shop_universe_index and catchment_km is not None and user_lat is not None and user_lng is not None:
        shops = shops_in_catchment(user_lat, user_lng, radius_km=catchment_km, universe=shop_universe_index)

    if not shops:
        return _hydrate_match(
            {
                "feasibility": "infeasible",
                "summary": "No shops within walking distance.",
                "shop_groups": [],
                "missing": [],
                "blockers": [
                    {
                        "ingredient": item.get("description") or "",
                        "suggestion": "Move closer to a Vitrina shop.",
                    }
                    for item in recipe.get("ingredients") or []
                ],
            },
            shops_index=shop_universe_index,
            products_index={},
            user_lat=user_lat,
            user_lng=user_lng,
            catchment_km=catchment_km,
            expanded_to_km=None,
            model_meta={"model": model, "ms": 0, "skipped": "no_shops_in_catchment"},
        )

    products_index: dict[str, dict[str, Any]] = {}
    shops_payload: list[dict[str, Any]] = []
    for shop in shops:
        items = product_universe_grouped.get(shop["id"], [])
        for product in items:
            products_index[product["id"]] = product
        shops_payload.append(_compact_shop_with_inventory(shop, items))

    prompt = _build_user_prompt(
        recipe,
        shops_payload,
        catchment_km=catchment_km,
        user_pin=(user_lat, user_lng)
        if user_lat is not None and user_lng is not None
        else None,
    )
    raw, meta = _call_openrouter(prompt, model=model, api_key=api_key, timeout=timeout)
    logger.info(
        "matcher.first_pass feasibility=%s shop_groups=%d blockers=%d ms=%d in=%s out=%s",
        raw.get("feasibility"),
        len(raw.get("shop_groups") or []),
        len(raw.get("blockers") or []),
        meta["ms"],
        meta.get("input_tokens"),
        meta.get("output_tokens"),
    )

    expanded_to_km: float | None = None
    if (
        auto_expand
        and raw.get("feasibility") == "partial"
        and catchment_km is not None
        and catchment_km < EXPANDED_CATCHMENT_KM
        and user_lat is not None
        and user_lng is not None
    ):
        wider_shops = shops_in_catchment(
            user_lat,
            user_lng,
            radius_km=EXPANDED_CATCHMENT_KM,
            universe=shop_universe_index,
        )
        wider_ids = {s["id"] for s in wider_shops}
        current_ids = {s["id"] for s in shops}
        if wider_ids - current_ids:
            logger.info(
                "matcher.expand from_km=%.1f to_km=%.1f extra_shops=%d",
                catchment_km,
                EXPANDED_CATCHMENT_KM,
                len(wider_ids - current_ids),
            )
            return match_recipe(
                recipe,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=EXPANDED_CATCHMENT_KM,
                shops=wider_shops,
                model=model,
                timeout=timeout,
                auto_expand=False,
            ) | {"expanded_from_km": catchment_km}

    return _hydrate_match(
        raw,
        shops_index=shop_universe_index,
        products_index=products_index,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
        expanded_to_km=expanded_to_km,
        model_meta=meta,
    )


# ---------------------------------------------------------------------------
# Per-user resolve (Stage B)
# ---------------------------------------------------------------------------


def _filter_universal_to_catchment(
    universal: dict[str, Any],
    *,
    user_lat: float,
    user_lng: float,
    catchment_km: float,
    shops_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    """Drop universal shop_groups whose shop is outside the user catchment.

    Returns the filtered match + the set of in-catchment shop ids that
    were retained, so the caller can decide whether a re-run with the
    in-catchment universe is worth its LLM cost.
    """
    in_catchment_ids: set[str] = set()
    for shop in shops_index.values():
        address = shop.get("address") or {}
        lat = address.get("lat")
        lng = address.get("lng")
        if lat is None or lng is None:
            continue
        if haversine_km(user_lat, user_lng, lat, lng) <= catchment_km:
            in_catchment_ids.add(shop["id"])

    kept_groups: list[dict[str, Any]] = []
    dropped_items: list[dict[str, Any]] = []
    total_minor = 0
    for group in universal.get("shop_groups") or []:
        shop_id = group["shop"]["id"]
        if shop_id in in_catchment_ids:
            shop_row = shops_index.get(shop_id, {})
            decorated = shop_with_distance(shop_row, user_lat, user_lng)
            kept = {**group, "shop": decorated}
            kept_groups.append(kept)
            total_minor += int(group.get("subtotal_minor") or 0)
        else:
            for item in group["items"]:
                dropped_items.append(
                    {
                        "ingredient": item.get("ingredient") or "",
                        "blocking": False,
                        "suggestion": (
                            f"Originally from {group['shop']['name']} — "
                            f"outside your {catchment_km:.1f} km catchment."
                        ),
                    }
                )

    feasibility = universal.get("feasibility") or "infeasible"
    if dropped_items and feasibility == "full":
        feasibility = "partial"

    return (
        {
            **universal,
            "feasibility": feasibility,
            "shop_groups": kept_groups,
            "missing": list(universal.get("missing") or []) + dropped_items,
            "total_eur": round(total_minor / 100, 2),
            "total_minor": total_minor,
            "catchment_km": catchment_km,
            "user_pin": {"lat": user_lat, "lng": user_lng},
        },
        in_catchment_ids,
    )


@lru_cache(maxsize=512)
def _resolve_cached(
    recipe_id: str,
    user_lat_cell: float,
    user_lng_cell: float,
    catchment_key: float,
) -> str:
    # Placeholder cache slot — populated by ``resolve_for_user``. We use
    # the outer function for arg parsing, this inner one just gives us a
    # bounded LRU keyed on a coarse (lat, lng) cell + catchment.
    raise RuntimeError("populated by resolve_for_user only")


def _coarse_cell(value: float, *, decimals: int = 3) -> float:
    """Round lat/lng to ~111m granularity so cache hits aren't GPS-jitter sensitive."""
    return round(value, decimals)


def resolve_for_user(
    recipe_id: str,
    universal: dict[str, Any],
    recipe: dict[str, Any],
    *,
    user_lat: float,
    user_lng: float,
    catchment_km: float = DEFAULT_CATCHMENT_KM,
    rerun_on_partial: bool = True,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Stage B — derive a per-user match from a pre-baked universal match.

    Cheap path: filter universal by catchment. Expensive path: when the
    filter degrades feasibility, re-run the matcher against just the
    in-catchment shops to look for substitutes that exist closer (the
    universal pass might have picked a perfect match in a far shop and
    overlooked an OK substitute next door).
    """
    shops_index = _all_shops_indexed()
    filtered, in_catchment_ids = _filter_universal_to_catchment(
        universal,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
        shops_index=shops_index,
    )
    if not rerun_on_partial:
        return filtered
    if filtered.get("feasibility") in {"full", "infeasible"}:
        return filtered
    if not in_catchment_ids:
        return filtered

    in_catchment_shops = [
        shop for sid, shop in shops_index.items() if sid in in_catchment_ids
    ]
    logger.info(
        "matcher.resolve.rerun recipe_id=%s in_catchment=%d (%.1fkm)",
        recipe_id,
        len(in_catchment_shops),
        catchment_km,
    )
    return match_recipe(
        recipe,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
        shops=in_catchment_shops,
        model=model,
        auto_expand=True,
    )


__all__ = [
    "DEFAULT_CATCHMENT_KM",
    "EXPANDED_CATCHMENT_KM",
    "match_recipe",
    "resolve_for_user",
    "shops_in_catchment",
    "haversine_km",
    "walking_minutes",
]
