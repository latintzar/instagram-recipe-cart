-- Migration 012: Magic-link token tables
--
-- Replaces the scattered ``chat_threads.metadata.location_short_slug`` /
-- ``location_token`` approach with dedicated tables that have proper
-- lifecycle (expires_at, redeemed_at) and clean FK semantics.
--
-- Both tables live in the ``public`` schema alongside ``chat_threads``,
-- ``carts``, etc. (the ``mcp`` schema used in earlier migration drafts
-- does not exist in this Supabase project).

-- ── Location tokens ─────────────────────────────────────────────────────
-- One row per ``location.create_link`` call. The web page resolves the slug
-- to a thread context, lets the shopper pin an area, then marks it redeemed.

CREATE TABLE IF NOT EXISTS public.location_tokens (
    slug        text        PRIMARY KEY,
    thread_id   text        NOT NULL,
    cart_key    text        NOT NULL,
    expires_at  timestamptz NOT NULL DEFAULT (now() + interval '10 minutes'),
    redeemed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_location_tokens_thread_id
    ON public.location_tokens (thread_id);

-- ── Checkout tokens ──────────────────────────────────────────────────────
-- One row per ``checkout.create_link`` call. Stores the basket references
-- so that the web page can (a) show the basket before auth, (b) create the
-- Stripe session after auth, and (c) be linked to the anonymous thread for
-- "where's my order?" follow-ups.

CREATE TABLE IF NOT EXISTS public.checkout_tokens (
    slug            text        PRIMARY KEY,
    cart_ids        text[]      NOT NULL,
    cart_key        text        NOT NULL,
    thread_id       text        NOT NULL,
    slot_payload    jsonb,
    expires_at      timestamptz NOT NULL DEFAULT (now() + interval '30 minutes'),
    redeemed_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_checkout_tokens_thread_id
    ON public.checkout_tokens (thread_id);

CREATE INDEX IF NOT EXISTS idx_checkout_tokens_cart_key
    ON public.checkout_tokens (cart_key);
