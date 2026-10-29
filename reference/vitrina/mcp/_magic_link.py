"""Shared slug-minting helper for all Vitrina magic links.

Both ``location.create_link`` and ``checkout.create_link`` generate short
URL-safe slugs that map to rows in ``mcp.location_tokens`` /
``mcp.checkout_tokens``. This module owns the mint-with-dedup logic so the
two callers stay DRY and the collision-retry behaviour is consistently tested.

Usage::

    from _magic_link import mint_unique_slug

    slug = mint_unique_slug(exists=lambda s: location_tokens.lookup(s) is not None)
"""

from __future__ import annotations

import secrets
import string
from typing import Callable

_ALPHABET = string.ascii_lowercase + string.digits
_MAX_RETRIES = 12


def mint_unique_slug(
    exists: Callable[[str], bool],
    *,
    length: int = 6,
) -> str:
    """Return a random ``length``-char alphanumeric slug not already in use.

    Args:
        exists: Callable that returns True if the candidate slug is already
            taken (e.g. a DB lookup). Called at most ``_MAX_RETRIES`` times.
        length: Slug length. Default 6 gives ~2.2 billion combinations —
            more than enough for a per-user, short-TTL token.

    Raises:
        RuntimeError: If a unique slug could not be found within the retry
            budget. This should never happen in practice but signals that
            something is badly wrong (e.g. the table was never cleaned up).
    """
    for _ in range(_MAX_RETRIES):
        slug = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if not exists(slug):
            return slug
    raise RuntimeError(
        f"mint_unique_slug: could not find a unique {length}-char slug in "
        f"{_MAX_RETRIES} tries — check token table cleanup."
    )
