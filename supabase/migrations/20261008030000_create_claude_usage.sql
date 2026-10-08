-- Claude API の使用額の記録（1回の呼び出しにつき1行）と、残高の目安のための設定。
--
-- 予想の保存データ（prediction_snapshots）はレースごとに最新の1件だけなので、同じレースで入力が変わって
-- 2回呼んだときに1回分が消える。そこで実際に API を呼ぶたびにここへ1行残し、合計は
-- claude_usage_summary() で Supabase 側で計算して数字だけを返す（アプリは行を取らない）。
-- 権限はほかの予想テーブルと同じく、追加と読み取りだけ（更新・削除はできない）。

create table if not exists public.claude_usage (
  id bigserial primary key,
  called_at timestamptz not null default now(),
  race_key text,
  purpose text not null default 'race_prediction',
  model text,
  input_tokens integer,
  output_tokens integer,
  cache_read_input_tokens integer,
  cache_creation_input_tokens integer,
  usd numeric(10, 5) not null,
  status text
);
create index if not exists claude_usage_called_at_idx on public.claude_usage (called_at);

alter table public.claude_usage enable row level security;
drop policy if exists claude_usage_app_insert on public.claude_usage;
create policy claude_usage_app_insert on public.claude_usage for insert to anon, authenticated with check (true);
drop policy if exists claude_usage_app_select on public.claude_usage;
create policy claude_usage_app_select on public.claude_usage for select to anon, authenticated using (true);

-- 今日・今月（日本時間）と、チャージした日以降の使用額（ドル）と回数。
create or replace function public.claude_usage_summary(p_since date default null)
returns json
language sql
stable
security invoker
set search_path = public
as $$
  with u as (
    select usd, (called_at at time zone 'Asia/Tokyo')::date as d from public.claude_usage
  ), t as (
    select (now() at time zone 'Asia/Tokyo')::date as today
  )
  select json_build_object(
    'today_usd', coalesce(sum(u.usd) filter (where u.d = t.today), 0),
    'today_calls', count(u.usd) filter (where u.d = t.today),
    'month_usd', coalesce(sum(u.usd) filter (where u.d >= date_trunc('month', t.today)::date), 0),
    'month_calls', count(u.usd) filter (where u.d >= date_trunc('month', t.today)::date),
    'since_usd', case when p_since is null then null
                      else coalesce(sum(u.usd) filter (where u.d >= p_since), 0) end,
    'since_calls', case when p_since is null then null else count(u.usd) filter (where u.d >= p_since) end
  )
  from t left join u on true
  group by t.today;
$$;
grant execute on function public.claude_usage_summary(date) to anon, authenticated;

-- チャージした金額（ドル）とチャージした日（設定タブで入力。残高の目安＝チャージ額 − その日以降の使用額）
alter table public.app_settings
  add column if not exists claude_credit_usd numeric(10, 2),
  add column if not exists claude_credit_date date;

-- この仕組みより前に予想の保存データに残っていた呼び出しを移す（同じものは二重に入れない）
insert into public.claude_usage (called_at, race_key, model, input_tokens, output_tokens,
                                 cache_read_input_tokens, cache_creation_input_tokens, usd, status)
select s.saved_at::timestamptz, s.race_key, c->>'model',
       (c->'usage'->>'input_tokens')::int, (c->'usage'->>'output_tokens')::int,
       (c->'usage'->>'cache_read_input_tokens')::int, (c->'usage'->>'cache_creation_input_tokens')::int,
       (c->'cost'->>'usd')::numeric, c->>'status'
from public.prediction_snapshots s
cross join lateral (select s.payload_json::jsonb->'claude' as c) x
where s.payload_json like '%"claude"%' and c->'cost'->>'usd' is not null
  and not exists (select 1 from public.claude_usage u where u.race_key = s.race_key and u.usd = (c->'cost'->>'usd')::numeric);
