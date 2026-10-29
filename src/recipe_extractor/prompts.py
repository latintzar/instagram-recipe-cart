"""Recipe JSON schema + LLM prompts for vision extraction and text synthesis.

The schema is deliberately structured so a downstream "ingredient matcher"
(map each ingredient to a buyable product) has clean, single-noun targets to
work with. Even if you don't do shop matching, the ``canonical`` / ``prep`` /
``quantity_g`` split gives you well-normalised ingredient rows for free.
"""

from __future__ import annotations

import json
from typing import Any

# Single source of truth for the ``recipe`` object shape.
#
# Every ingredient row carries:
#   - ``description``   — what the cook reads ("2 large tomatoes, diced")
#   - ``canonical``     — the *single* canonical noun an ingredient matcher
#                         can search on ("tomato"). Always unbranded,
#                         singular, lowercase, no prep verbs, no qualifiers.
#                         Combination ingredients ("pico de gallo", "garam
#                         masala", "Italian seasoning") get decomposed into
#                         one row per buyable component.
#   - ``prep``          — preparation note kept separate from canonical
#                         ("chopped" / "grated") so it never pollutes a
#                         search target.
#   - ``quantity_g``    — best-effort grams (or ml, treated as grams for
#                         water-like liquids). null only for piece-only
#                         items (1 lemon, 2 star anise).
#   - ``optional``      — true when the line is a garnish / "to taste".
RECIPE_JSON_SCHEMA_BLOCK = """{
  "title": "string",
  "description": "string",
  "source_url": "string",
  "source_platform": "instagram|tiktok|youtube|facebook|pinterest|unknown",
  "servings": {"quantity": null, "evidence": "string"},
  "duration": {"quantity": null, "units": "minutes", "evidence": "string"},
  "ingredients": [
    {
      "description": "as the recipe says it (e.g. '2 cloves garlic, minced')",
      "canonical": "single unbranded noun to search on (e.g. 'garlic')",
      "prep": "chopping/cutting verb if any (e.g. 'minced', 'thinly sliced'); else empty",
      "quantity": null,
      "units": "",
      "quantity_g": null,
      "optional": false,
      "confidence": 0.0,
      "evidence": "frame/metadata clue"
    }
  ],
  "equipment": ["string"],
  "directions": [
    {"step": 1, "text": "instruction", "evidence": "frame/metadata clue"}
  ],
  "tips": ["string"],
  "unknowns": ["string"],
  "confidence": 0.0
}"""


def compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "webpage_url",
        "original_url",
        "title",
        "description",
        "duration",
        "uploader",
        "channel",
        "upload_date",
        "tags",
        "categories",
    ]
    return {key: metadata.get(key) for key in keys if metadata.get(key) not in (None, "", [])}


def vision_extraction_prompt(video: dict[str, str], metadata: dict[str, Any], frame_count: int) -> str:
    """Prompt for extracting a recipe from video frames (primary path)."""

    return f"""
You are extracting a cooking recipe from a short social video.

Use frames as primary evidence. Use metadata only as weak context.
Return JSON only.

Ingredient extraction rules (this is the part shoppers see — do it well):

1. **Decompose combination ingredients.** A single line like "pico de
   gallo", "salsa fresca", "Italian seasoning", "ras el hanout", "five
   spice", "Greek yogurt sauce", "guacamole", "pesto", "BBQ sauce", or
   any branded blend MUST be split into one row per buyable component
   the cook would actually grab from a shelf. "1 cup pico de gallo"
   becomes four rows: tomato, white onion, cilantro, lime juice. If
   you're unsure of the exact split, prefer over-decomposing — a matcher
   and the shopper both win when each row is a single SKU.

2. **Canonical noun discipline.** For every row, fill ``canonical``
   with one unbranded singular lowercase English noun phrase. Strip
   leading quantities ("2 cups"), brand names ("Heinz", "Lurpak"),
   prep verbs ("chopped"), and decorative qualifiers ("fresh",
   "free-range", "organic", "extra-virgin"). Examples:
       "2 large yellow onions, diced" → canonical="yellow onion"
       "1 lb ground beef (85/15)"     → canonical="ground beef"
       "Heinz ketchup, ½ cup"         → canonical="ketchup"
       "fresh basil leaves, torn"     → canonical="basil"
       "extra-virgin olive oil"       → canonical="olive oil"
   Use *cooking-grade English* even when the video is in another
   language; ``description`` keeps the on-screen wording.

3. **Prep notes go in ``prep``, never in ``canonical``.** "minced",
   "thinly sliced", "grated", "julienned", "deveined", "torn", "cubed",
   "1-inch dice", etc.

4. **Quantities — never invent.** If the frames / caption do not
   give an exact quantity, leave ``quantity`` and ``units`` null and
   record the uncertainty in ``evidence``. But ALWAYS try to fill
   ``quantity_g`` (or ml, equivalent) with a *culinary-common-sense*
   estimate from the dish + servings — even when the recipe is silent.
   Conversion guide:
       1 cup flour ≈ 125 g           1 lb ≈ 454 g
       1 cup water ≈ 240 g           1 oz ≈ 28 g
       1 tbsp oil  ≈ 14 g            1 tsp salt ≈ 6 g
       1 medium tomato ≈ 120 g       1 medium onion ≈ 150 g
       1 garlic clove ≈ 4 g          1 lemon (juice) ≈ 30 g
   Set ``quantity_g`` null only for true piece-only items where grams
   are nonsense (eg. "2 star anise", "1 bay leaf").

5. **Mark optional lines.** Garnishes, "salt and pepper to taste",
   "a pinch of red-pepper flakes", and "for serving" rows go with
   ``optional: true``.

6. **One canonical noun per row.** "Salt and pepper" is two rows.
   "Olive oil + butter" is two rows. Keep each row indivisible.

Use frames as primary evidence. Use metadata as weak context. If a
quantity is uncertain, set ``quantity``/``units`` null and write the
uncertainty in ``evidence`` — but still emit ``quantity_g`` when
culinary common sense gives you one.

Video: {json.dumps(video, ensure_ascii=False)}
Metadata: {json.dumps(compact_metadata(metadata), ensure_ascii=False)}
Frames supplied: {frame_count}

Return this schema:
{RECIPE_JSON_SCHEMA_BLOCK}
""".strip()


def synthesis_user_prompt(
    brief: str,
    *,
    dietary_notes: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "shopper_brief": brief.strip(),
        "dietary_or_constraints": (dietary_notes or "").strip() or None,
    }
    return (
        "Invent a plausible home-cooking recipe that fits the brief. "
        "You did NOT watch a video — be honest in evidence fields (say 'inferred').\n\n"
        "Apply the same ingredient discipline as the extraction prompt: "
        "decompose combination ingredients (no 'pico de gallo' / 'Italian seasoning' / "
        "'Greek yogurt sauce' as a single row), fill ``canonical`` with one unbranded "
        "singular lowercase English noun per row, push prep verbs into ``prep``, and "
        "always estimate ``quantity_g`` via culinary common sense even when ``quantity`` "
        "is left null.\n\n"
        "Return JSON only matching the schema.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def synthesis_system_prompt() -> str:
    return (
        "You are a recipe author. Output a single JSON object matching "
        "exactly this schema (no markdown fences):\n"
        f"{RECIPE_JSON_SCHEMA_BLOCK}\n"
        "Use source_platform 'unknown' and source_url empty string when not from a URL."
    )
