"""Thin Supabase REST client for the Vitrina UCP backend.

This wrapper intentionally avoids the official ``supabase-py`` SDK to
keep the dependency graph small (we already have ``httpx``) and to make
the surface trivial to mock in tests. It speaks directly to the
PostgREST-backed ``/rest/v1/`` endpoint using the service role key, so
every caller is assumed to be server-side (never embed this client in
user-facing code).

Shared across the A4/A5/A6/A7/A8 UCP subagents — each cart / checkout /
recipe / thread handler module imports :func:`get_supabase` to talk to
the same tables.

Query chain shape (chosen to roughly match the supabase-py ergonomics so
the shape is already familiar)::

    sb.table("carts").select("*").eq("id", cart_id).single().execute()
    sb.table("cart_line_items").insert({...}).execute()
    sb.table("user_recipe_matches").upsert({...}, on_conflict="device_id,recipe_id").execute()
    sb.table("carts").update({"status": "closed"}).eq("id", cid).execute()

Tests monkeypatch ``Query.execute`` via ``fixtures/supabase_mock.py``
(an in-memory dict-of-lists backend with the same chain surface), so no
real HTTP is ever issued from the test suite.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import httpx


class SupabaseError(RuntimeError):
    """Raised when the Supabase REST API returns a non-2xx status."""


class Supabase:
    """Entry point. One instance per process is fine — see ``get_supabase``."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        self.url = (url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        # Canonical name across the polyproject is SUPABASE_SERVICE_ROLE_KEY
        # (see packages/core/src/core/db.py). Older Vitrina deploys also
        # populate SUPABASE_SERVICE_KEY, so accept either to make this robust
        # to env-var naming drift between Railway services.
        self.key = (
            key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_SERVICE_KEY")
            or ""
        )
        if not self.url or not self.key:
            missing = []
            if not self.url:
                missing.append("SUPABASE_URL")
            if not self.key:
                missing.append("SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY)")
            raise RuntimeError(
                "supabase_not_configured: missing " + ", ".join(missing)
            )

    def table(self, name: str) -> "Table":
        return Table(self, name)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _endpoint(self, table: str) -> str:
        return f"{self.url}/rest/v1/{table}"


class Table:
    def __init__(self, client: Supabase, name: str) -> None:
        self.client = client
        self.name = name

    def select(self, columns: str = "*") -> "Query":
        return Query(self, method="GET", params={"select": columns})

    def insert(self, row: dict | list[dict]) -> "Query":
        return Query(self, method="POST", body=row, prefer="return=representation")

    def upsert(
        self,
        row: dict | list[dict],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
    ) -> "Query":
        params = {"on_conflict": on_conflict} if on_conflict else None
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        return Query(
            self,
            method="POST",
            params=params,
            body=row,
            prefer=f"resolution={resolution},return=representation",
        )

    def update(self, patch: dict) -> "Query":
        return Query(self, method="PATCH", body=patch, prefer="return=representation")

    def delete(self) -> "Query":
        return Query(self, method="DELETE", prefer="return=representation")


class Query:
    def __init__(
        self,
        table: Table,
        *,
        method: str,
        params: dict[str, str] | None = None,
        body: Any = None,
        prefer: str | None = None,
    ) -> None:
        self.table = table
        self.method = method
        self.params: dict[str, str] = dict(params or {})
        self.body = body
        self.prefer = prefer
        self._single = False

    # ── filters ──────────────────────────────────────────────────────
    def eq(self, col: str, val: Any) -> "Query":
        self.params[col] = f"eq.{_fmt(val)}"
        return self

    def neq(self, col: str, val: Any) -> "Query":
        self.params[col] = f"neq.{_fmt(val)}"
        return self

    def is_(self, col: str, val: Any) -> "Query":
        # PostgREST is.null / is.true / is.false
        self.params[col] = f"is.{val if val is not None else 'null'}"
        return self

    def in_(self, col: str, vals: Iterable[Any]) -> "Query":
        joined = ",".join(_fmt(v) for v in vals)
        self.params[col] = f"in.({joined})"
        return self

    def gt(self, col: str, val: Any) -> "Query":
        self.params[col] = f"gt.{_fmt(val)}"
        return self

    def gte(self, col: str, val: Any) -> "Query":
        self.params[col] = f"gte.{_fmt(val)}"
        return self

    def lt(self, col: str, val: Any) -> "Query":
        self.params[col] = f"lt.{_fmt(val)}"
        return self

    def lte(self, col: str, val: Any) -> "Query":
        self.params[col] = f"lte.{_fmt(val)}"
        return self

    def order(self, col: str, desc: bool = False) -> "Query":
        self.params["order"] = f"{col}.{'desc' if desc else 'asc'}"
        return self

    def limit(self, n: int) -> "Query":
        self.params["limit"] = str(n)
        return self

    def single(self) -> "Query":
        self._single = True
        return self

    def maybe_single(self) -> "Query":
        """Like ``single()`` but returns None instead of raising when no row found."""
        self._single = True
        return self

    # ── execution ────────────────────────────────────────────────────
    def execute(self) -> Any:
        headers = self.table.client._headers(
            {"Prefer": self.prefer} if self.prefer else None
        )
        if self._single:
            headers["Accept"] = "application/vnd.pgrst.object+json"
        url = self.table.client._endpoint(self.table.name)
        resp = httpx.request(
            self.method,
            url,
            params=self.params,
            json=self.body,
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code == 406 and self._single:
            # PostgREST returns 406 when no row matches and Accept: vnd.pgrst.object
            # (i.e. ``maybe_single()``-style query). Return None instead of raising.
            return None
        if resp.status_code >= 400:
            raise SupabaseError(
                f"{self.table.name} {self.method} {resp.status_code}: {resp.text}"
            )
        if resp.status_code == 204 or not resp.content:
            return [] if not self._single else None
        try:
            return resp.json()
        except ValueError:
            return resp.text


def _fmt(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


_singleton: Supabase | None = None


def get_supabase() -> Supabase:
    """Return a process-wide :class:`Supabase` client, constructing once."""
    global _singleton
    if _singleton is None:
        _singleton = Supabase()
    return _singleton


def reset_singleton() -> None:
    """Test hook — drop the cached client so monkeypatched env takes effect."""
    global _singleton
    _singleton = None
