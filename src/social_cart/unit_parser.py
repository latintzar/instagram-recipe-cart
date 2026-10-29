"""Deterministic pack-size parser.

Converts ``unit`` + ``unit_size`` strings (Bulgarian + English) on a
product row into either grams or piece-counts. Used by the matcher
hydration path so cart lines carry ``pack_grams`` and the leftover-
driven stretch logic can do real arithmetic instead of guessing.

Examples:
    parse_pack_grams("kg", "1кг")       -> {"grams": 1000, "pieces": None}
    parse_pack_grams("pack", "500g")    -> {"grams": 500,  "pieces": None}
    parse_pack_grams("pack", "4 pcs")   -> {"grams": None, "pieces": 4}
    parse_pack_grams("pc", "1 pc")      -> {"grams": None, "pieces": 1}
    parse_pack_grams("pc", "200мл")     -> {"grams": 200,  "pieces": None}  # ml ~ g for liquids

We treat millilitres as grams (water density = 1, close enough for
shopping-list purposes — broth, milk, oil, etc.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PackSize:
    grams: float | None
    pieces: int | None


_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")

# Order matters — match longest unit first.
_GRAM_UNITS = (
    ("кг", 1000.0),
    ("kg", 1000.0),
    ("г", 1.0),
    ("g", 1.0),
    ("л", 1000.0),
    ("l", 1000.0),
    ("мл", 1.0),
    ("ml", 1.0),
)

_PIECE_KEYWORDS = ("pcs", "pc", "pack", "slab", "bunch", "бр", "брой")


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def parse_pack_grams(unit: str | None, unit_size: str | None) -> PackSize:
    """Best-effort conversion to grams or piece-count.

    Strategy:
      1. Try to extract grams from ``unit_size`` (handles "500г", "1.5 kg",
         "200мл").
      2. Otherwise treat ``unit_size`` as a piece-count (e.g. "4 pcs").
      3. Fall back to ``unit`` when ``unit_size`` is empty.
    """
    size = (unit_size or "").strip().lower()
    unit_lc = (unit or "").strip().lower()

    if size:
        match = _NUMBER.search(size)
        if match:
            n = _to_float(match.group(1))
            tail = size[match.end():].strip()
            if n is not None:
                # Longest gram-unit match wins.
                for token, factor in sorted(_GRAM_UNITS, key=lambda x: -len(x[0])):
                    if tail.startswith(token):
                        return PackSize(grams=n * factor, pieces=None)
                # Numeric + piece keyword (e.g. "4 pcs", "1 pc", "1 slab").
                if any(kw in tail for kw in _PIECE_KEYWORDS):
                    return PackSize(grams=None, pieces=int(n) if n.is_integer() else None)
                # Numeric with no unit — bare "4" + unit="pack".
                if unit_lc in _PIECE_KEYWORDS:
                    return PackSize(grams=None, pieces=int(n) if n.is_integer() else None)

    # No usable size string — derive from unit alone.
    if unit_lc == "kg":
        return PackSize(grams=1000.0, pieces=None)
    if unit_lc in {"pc", "pcs"}:
        return PackSize(grams=None, pieces=1)
    return PackSize(grams=None, pieces=None)
