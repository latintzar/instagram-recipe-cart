-- ============================================================================
-- Vitrina — 001_commerce_tables.sql
--
-- Phase-1 commerce schema: shops, products, carts, checkout, orders, chat,
-- agent error tickets. Enables RLS (defence-in-depth; REST layer runs under
-- service_role and enforces cart_key for anonymous users) and wires up the
-- Slack webhook trigger for agent_errors via pg_net.
--
-- Rerun-safe: uses `create extension if not exists`, `create table` (first
-- run only), and idempotent policy drops. Wrap the whole file in a tx.
-- ============================================================================

begin;

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
create extension if not exists pgcrypto;
create extension if not exists pg_net;

-- ---------------------------------------------------------------------------
-- app_settings (runtime config for triggers, e.g. Slack webhook URL)
-- ---------------------------------------------------------------------------
create table if not exists app_settings (
  key text primary key,
  value text not null
);

-- The original operating alert-hook setting is omitted from this export.

-- ---------------------------------------------------------------------------
-- shops
-- ---------------------------------------------------------------------------
create table if not exists shops (
  id text primary key,
  name text not null,
  timezone text not null,
  opening_hours jsonb not null,   -- {"mon":[["09:00","20:00"]],...,"sun":[]}
  address text,
  lat double precision,
  lng double precision,
  image_url text,
  currency text not null default 'EUR',
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
create table if not exists products (
  id text primary key,
  shop_id text not null references shops(id) on delete cascade,
  name text not null,
  description text,
  category text,
  price_eur numeric(10,2) not null,
  price_minor int generated always as (round(price_eur * 100)::int) stored,
  image_url text,
  unit text,
  stock int not null default 0,
  currency text not null default 'EUR',
  created_at timestamptz not null default now()
);
create index if not exists products_shop_idx on products(shop_id);

-- ---------------------------------------------------------------------------
-- carts
-- ---------------------------------------------------------------------------
create table if not exists carts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id),
  cart_key text,
  shop_id text not null references shops(id),
  currency text not null,
  status text not null default 'open',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint cart_owner_present check (user_id is not null or cart_key is not null)
);
create index if not exists carts_user_open_idx on carts(user_id, status) where status='open';
create index if not exists carts_key_open_idx  on carts(cart_key, status) where status='open';

-- ---------------------------------------------------------------------------
-- cart_line_items
-- ---------------------------------------------------------------------------
create table if not exists cart_line_items (
  id uuid primary key default gen_random_uuid(),
  cart_id uuid not null references carts(id) on delete cascade,
  product_id text not null,
  quantity int not null check (quantity > 0),
  base_amount_minor int not null,
  discount_minor int not null default 0,
  subtotal_minor int not null,
  tax_minor int not null default 0,
  total_minor int not null,
  product_snapshot jsonb not null,
  group_id text,
  group_metadata jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists cart_line_items_cart_idx on cart_line_items(cart_id);
create index if not exists cart_line_items_group_idx on cart_line_items(cart_id, group_id) where group_id is not null;

-- ---------------------------------------------------------------------------
-- checkout_sessions
-- ---------------------------------------------------------------------------
create table if not exists checkout_sessions (
  id uuid primary key default gen_random_uuid(),
  cart_id uuid not null references carts(id),
  group_id uuid,
  status text not null,
  stripe_session_id text,
  stripe_checkout_url text,
  fulfillment_option_id text default 'pickup_in_store',
  requires_escalation_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  canceled_at timestamptz
);
create index if not exists checkout_sessions_group_idx on checkout_sessions(group_id) where group_id is not null;
create index if not exists checkout_sessions_cart_idx on checkout_sessions(cart_id);

-- ---------------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------------
create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  checkout_session_id uuid not null references checkout_sessions(id),
  group_id uuid,
  cart_id uuid not null references carts(id),
  user_id uuid references auth.users(id),
  shop_id text not null references shops(id),
  currency text not null,
  line_items jsonb not null,
  totals jsonb not null,
  collection_code text not null,
  collect_key text not null,
  fulfillment_status text not null default 'awaiting_collection',
  paid_at timestamptz not null default now(),
  collected_at timestamptz,
  collected_via text,
  permalink_url text not null
);
create index if not exists orders_user_idx on orders(user_id, paid_at desc);
create index if not exists orders_awaiting_idx on orders(fulfillment_status, paid_at) where fulfillment_status='awaiting_collection';
create unique index if not exists orders_collect_key_idx on orders(collect_key);

-- ---------------------------------------------------------------------------
-- chat_threads
-- ---------------------------------------------------------------------------
create table if not exists chat_threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id),
  cart_key text,
  title text,
  pinned boolean not null default false,
  seeded_from_order_id uuid references orders(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint thread_owner_present check (user_id is not null or cart_key is not null)
);
create index if not exists chat_threads_user_idx on chat_threads(user_id, updated_at desc);
create index if not exists chat_threads_key_idx on chat_threads(cart_key, updated_at desc);

-- ---------------------------------------------------------------------------
-- chat_messages
-- ---------------------------------------------------------------------------
create table if not exists chat_messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references chat_threads(id) on delete cascade,
  role text not null,
  content jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists chat_messages_thread_idx on chat_messages(thread_id, created_at);

-- ---------------------------------------------------------------------------
-- agent_errors
-- ---------------------------------------------------------------------------
create table if not exists agent_errors (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid references chat_threads(id),
  user_id uuid references auth.users(id),
  cart_key text,
  cart_id uuid references carts(id),
  tool_name text not null,
  args jsonb not null,
  error_message text not null,
  stack text,
  recoverable boolean not null default true,
  posthog_event_id text,
  resolved_at timestamptz,
  resolved_by uuid references auth.users(id),
  resolution_note text,
  created_at timestamptz not null default now()
);
create index if not exists agent_errors_unresolved_idx on agent_errors(resolved_at, created_at) where resolved_at is null;

-- ===========================================================================
-- Row Level Security
-- ---------------------------------------------------------------------------
-- Model: the Vitrina REST layer connects using the `service_role` key and
-- validates `cart_key` in application code for anonymous sessions. RLS here
-- is defence-in-depth for any accidental anon/authenticated direct access.
-- ===========================================================================

alter table shops              enable row level security;
alter table products           enable row level security;
alter table carts              enable row level security;
alter table cart_line_items    enable row level security;
alter table checkout_sessions  enable row level security;
alter table orders             enable row level security;
alter table chat_threads       enable row level security;
alter table chat_messages      enable row level security;
alter table agent_errors       enable row level security;
alter table app_settings       enable row level security;

-- ---- shops: public catalogue, service-role writes -------------------------
drop policy if exists shops_select_public       on shops;
drop policy if exists shops_service_role_all    on shops;
create policy shops_select_public    on shops for select to anon, authenticated using (true);
create policy shops_service_role_all on shops for all    to service_role using (true) with check (true);

-- ---- products: public catalogue, service-role writes ----------------------
drop policy if exists products_select_public    on products;
drop policy if exists products_service_role_all on products;
create policy products_select_public    on products for select to anon, authenticated using (true);
create policy products_service_role_all on products for all    to service_role using (true) with check (true);

-- ---- carts: owner-only (by auth.uid); service_role bypasses ---------------
drop policy if exists carts_select_own        on carts;
drop policy if exists carts_insert_own        on carts;
drop policy if exists carts_update_own        on carts;
drop policy if exists carts_service_role_all  on carts;
create policy carts_select_own       on carts for select to authenticated using (auth.uid() = user_id);
create policy carts_insert_own       on carts for insert to authenticated with check (auth.uid() = user_id);
create policy carts_update_own       on carts for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy carts_service_role_all on carts for all    to service_role using (true) with check (true);

-- ---- cart_line_items: via parent cart -------------------------------------
drop policy if exists cart_line_items_select_own       on cart_line_items;
drop policy if exists cart_line_items_insert_own       on cart_line_items;
drop policy if exists cart_line_items_update_own       on cart_line_items;
drop policy if exists cart_line_items_delete_own       on cart_line_items;
drop policy if exists cart_line_items_service_role_all on cart_line_items;
create policy cart_line_items_select_own on cart_line_items for select to authenticated
  using (exists (select 1 from carts c where c.id = cart_line_items.cart_id and c.user_id = auth.uid()));
create policy cart_line_items_insert_own on cart_line_items for insert to authenticated
  with check (exists (select 1 from carts c where c.id = cart_line_items.cart_id and c.user_id = auth.uid()));
create policy cart_line_items_update_own on cart_line_items for update to authenticated
  using (exists (select 1 from carts c where c.id = cart_line_items.cart_id and c.user_id = auth.uid()))
  with check (exists (select 1 from carts c where c.id = cart_line_items.cart_id and c.user_id = auth.uid()));
create policy cart_line_items_delete_own on cart_line_items for delete to authenticated
  using (exists (select 1 from carts c where c.id = cart_line_items.cart_id and c.user_id = auth.uid()));
create policy cart_line_items_service_role_all on cart_line_items for all to service_role using (true) with check (true);

-- ---- checkout_sessions: via parent cart -----------------------------------
drop policy if exists checkout_sessions_select_own       on checkout_sessions;
drop policy if exists checkout_sessions_service_role_all on checkout_sessions;
create policy checkout_sessions_select_own on checkout_sessions for select to authenticated
  using (exists (select 1 from carts c where c.id = checkout_sessions.cart_id and c.user_id = auth.uid()));
create policy checkout_sessions_service_role_all on checkout_sessions for all to service_role using (true) with check (true);

-- ---- orders: owner-only; service_role bypasses ----------------------------
drop policy if exists orders_select_own       on orders;
drop policy if exists orders_service_role_all on orders;
create policy orders_select_own       on orders for select to authenticated using (auth.uid() = user_id);
create policy orders_service_role_all on orders for all    to service_role using (true) with check (true);

-- ---- chat_threads: owner-only ---------------------------------------------
drop policy if exists chat_threads_select_own       on chat_threads;
drop policy if exists chat_threads_insert_own       on chat_threads;
drop policy if exists chat_threads_update_own       on chat_threads;
drop policy if exists chat_threads_service_role_all on chat_threads;
create policy chat_threads_select_own       on chat_threads for select to authenticated using (auth.uid() = user_id);
create policy chat_threads_insert_own       on chat_threads for insert to authenticated with check (auth.uid() = user_id);
create policy chat_threads_update_own       on chat_threads for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy chat_threads_service_role_all on chat_threads for all    to service_role using (true) with check (true);

-- ---- chat_messages: via parent thread -------------------------------------
drop policy if exists chat_messages_select_own       on chat_messages;
drop policy if exists chat_messages_insert_own       on chat_messages;
drop policy if exists chat_messages_service_role_all on chat_messages;
create policy chat_messages_select_own on chat_messages for select to authenticated
  using (exists (select 1 from chat_threads t where t.id = chat_messages.thread_id and t.user_id = auth.uid()));
create policy chat_messages_insert_own on chat_messages for insert to authenticated
  with check (exists (select 1 from chat_threads t where t.id = chat_messages.thread_id and t.user_id = auth.uid()));
create policy chat_messages_service_role_all on chat_messages for all to service_role using (true) with check (true);

-- ---- agent_errors: user sees their own; service_role writes/updates -------
drop policy if exists agent_errors_select_own       on agent_errors;
drop policy if exists agent_errors_service_role_all on agent_errors;
create policy agent_errors_select_own       on agent_errors for select to authenticated using (auth.uid() = user_id);
create policy agent_errors_service_role_all on agent_errors for all    to service_role using (true) with check (true);

-- ---- app_settings: service_role only --------------------------------------
drop policy if exists app_settings_service_role_all on app_settings;
create policy app_settings_service_role_all on app_settings for all to service_role using (true) with check (true);

-- ===========================================================================
-- Slack webhook trigger for agent_errors
-- ---------------------------------------------------------------------------
-- Posts a Slack Incoming-Webhook message on every new agent_errors row.
-- Webhook URL is read from app_settings.slack_agent_errors_webhook; if empty
-- the trigger no-ops (safe default until the secret is provisioned).
-- ===========================================================================

create or replace function notify_slack_agent_error()
returns trigger language plpgsql security definer as $$
declare url text; payload jsonb;
begin
  select value into url from app_settings where key='slack_agent_errors_webhook';
  if url is null or url = '' then return new; end if;
  payload := jsonb_build_object('text', format(
    ':warning: Vitrina agent error: *%s*\nticket `%s`\nuser `%s`\nthread `%s`\n```%s```',
    new.tool_name, new.id, coalesce(new.user_id::text, new.cart_key), new.thread_id, new.error_message));
  perform net.http_post(url := url, body := payload::text,
    headers := jsonb_build_object('Content-Type','application/json'));
  return new;
end $$;

drop trigger if exists trg_agent_errors_slack on agent_errors;
create trigger trg_agent_errors_slack after insert on agent_errors
for each row execute procedure notify_slack_agent_error();

commit;
