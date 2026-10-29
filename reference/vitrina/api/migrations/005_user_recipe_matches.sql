-- 005_user_recipe_matches.sql
--
-- Per-user prematch cache for the Discover seed pool. The 40-row pool
-- in `recipe_videos/creator_catalog.json` ships with a `match_universal`
-- (every shop in our index, regardless of geography). To make Discover
-- feel instant for a brand-new user we run that universal match through
-- the per-user catchment filter ONCE per (user, recipe) pair the moment
-- we know the user's location, then cache the resolved match here.
--
-- Plan reference: vitrina-async-chat-and-seed-pool, phase C1.
--
-- Lifecycle (`status`):
--   queued    -> the prematch worker has the pair on its queue
--   running   -> worker is currently calling shop_matcher
--   resolved  -> match_json is the per-user filtered match (renderable)
--   skipped   -> nothing buyable in this user's catchment, hide the row
--   failed    -> matcher errored; UI keeps the row but falls back to
--                the universal (best-effort) match
--
-- We key on (device_id, recipe_id) — anonymous users get a stable
-- device_id from the frontend (uuid in localStorage); signed-in users
-- pass their auth.users.id as device_id so the same cache works pre-
-- and post-login. user_id, when present, lets RLS scope reads to the
-- signed-in user; anonymous rows are readable only by service_role.
create table if not exists user_recipe_matches (
  id uuid primary key default gen_random_uuid(),
  device_id text not null,
  user_id uuid references auth.users(id),
  recipe_id text not null,
  status text not null default 'queued',
  match_json jsonb,
  -- Snapshot of the catchment lat/lng/km used so a moved pin invalidates.
  pin_lat double precision,
  pin_lng double precision,
  catchment_km double precision,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz,
  error_message text,
  constraint user_recipe_matches_status_chk
    check (status in ('queued', 'running', 'resolved', 'skipped', 'failed')),
  constraint user_recipe_matches_device_recipe_uk
    unique (device_id, recipe_id)
);

create index if not exists user_recipe_matches_device_idx
  on user_recipe_matches(device_id, status);
create index if not exists user_recipe_matches_user_idx
  on user_recipe_matches(user_id) where user_id is not null;
create index if not exists user_recipe_matches_status_idx
  on user_recipe_matches(status, updated_at);

alter table user_recipe_matches enable row level security;

drop policy if exists user_recipe_matches_select_own        on user_recipe_matches;
drop policy if exists user_recipe_matches_service_role_all  on user_recipe_matches;
create policy user_recipe_matches_select_own on user_recipe_matches for select to authenticated
  using (user_id = auth.uid());
create policy user_recipe_matches_service_role_all on user_recipe_matches for all to service_role
  using (true) with check (true);
