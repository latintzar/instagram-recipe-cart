"""Original Vitrina recipe-to-purchase pack calculations."""
import math
import re
from typing import Any
from .unit_parser import parse_pack_grams
_WEIGHT_PURCHASE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(kg|кг|ml|мл|g|г|l|л)\b", re.IGNORECASE)

def _purchase_string_is_weight_dominant(purchase: str) -> bool:
    return bool(_WEIGHT_PURCHASE_RE.search((purchase or '').strip()))

def _grams_from_weight_purchase(purchase: str) -> float | None:
    m = _WEIGHT_PURCHASE_RE.search((purchase or '').strip())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    if n <= 0:
        return None
    unit = m.group(2).lower()
    if unit in {'kg', 'кг'}:
        return n * 1000.0
    if unit in {'l', 'л'}:
        return n * 1000.0
    return n

def _effective_pack_grams(item: dict[str, Any], product: dict[str, Any]) -> float | None:
    pg = item.get('pack_grams')
    try:
        if pg is not None:
            v = float(pg)
            if v > 0:
                return v
    except (TypeError, ValueError):
        pass
    ps = parse_pack_grams(product.get('unit') if isinstance(product.get('unit'), str) else None, product.get('unit_size') if isinstance(product.get('unit_size'), str) else None)
    if ps.grams is not None and ps.grams > 0:
        return float(ps.grams)
    return None

def _piece_pack_count(product: dict[str, Any]) -> int | None:
    text = str(product.get('unit_size') or '').lower()
    if not re.search('(pc|pcs|piece|pieces|бр|брой)', text):
        return None
    m = re.search('(\\d+(?:[.,]\\d+)?)', text)
    if not m:
        return 1
    try:
        return max(1, int(float(m.group(1).replace(',', '.'))))
    except ValueError:
        return 1

def _recipe_count(item: dict[str, Any]) -> float | None:
    raw = item.get('quantity_recipe')
    if raw is None:
        raw = item.get('quantity')
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            v = float(raw)
            return v if v > 0 else None
    except (TypeError, ValueError):
        pass
    if isinstance(raw, str):
        m = re.search('(\\d+(?:[.,]\\d+)?)', raw.strip())
        if m:
            try:
                v = float(m.group(1).replace(',', '.'))
                return v if v > 0 else None
            except ValueError:
                return None
    return None

def _purchase_units_for(item: dict[str, Any], portion_scale: float) -> int:
    """Best-effort number of sellable units needed for a recipe item."""
    product = item.get('product') if isinstance(item.get('product'), dict) else {}
    if not isinstance(product, dict):
        product = {}
    try:
        scale = float(portion_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    pack_g = _effective_pack_grams(item, product)
    qg = item.get('quantity_g')
    try:
        qgf = float(qg) if qg is not None else None
    except (TypeError, ValueError):
        qgf = None
    if qgf is not None and qgf > 0 and (pack_g is not None) and (pack_g > 0):
        return max(1, math.ceil(qgf * scale / pack_g))
    purchase = str(item.get('quantity_purchase') or '').strip()
    if purchase and _purchase_string_is_weight_dominant(purchase):
        need = _grams_from_weight_purchase(purchase)
        if need is not None and need > 0 and (pack_g is not None) and (pack_g > 0):
            return max(1, math.ceil(need * scale / pack_g))
    if purchase and (not _purchase_string_is_weight_dominant(purchase)):
        m = re.search('(\\d+(?:[.,]\\d+)?)', purchase)
        if m:
            try:
                n = float(m.group(1).replace(',', '.'))
                if n > 0:
                    return max(1, math.ceil(n * scale))
            except ValueError:
                pass
    rc = _recipe_count(item)
    pieces = _piece_pack_count(product)
    if rc is not None and rc > 0 and (pieces is not None) and (pieces > 0):
        return max(1, math.ceil(rc * scale / pieces))
    return 1
