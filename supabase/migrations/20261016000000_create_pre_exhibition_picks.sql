-- 「🌅 展示前厳選10」（pre_picks.py）の保存先。
--
--   pre_exhibition_picks : 1レース1行（毎朝10行）。展示前モデルの予想・買い目、展示後の予想（今の新モデル）、結果
--   pre_exhibition_runs  : 1日1行。使った版・集計表の日付・候補数・全候補の的中確率・設定
--
-- 書くのは GitHub Actions（service_role）と、下のトリガー（security definer）だけ。
-- アプリ（Publishable key）には読み取りだけ許す（管理者だけに表示）。
--
-- 展示後: アプリか自動固定がそのレースの予想を prediction_snapshots に保存すると、トリガーが
-- その予想（本命・的中確率・買い目）を post_* に書き、展示前と比べて
--   本命が変わった、または的中確率が10ポイント以上下がった
-- レースに post_flag を付ける（外さない）。結果（的中・払戻）は翌朝の pre_picks.py が書く。

create table if not exists public.pre_exhibition_picks (
  race_key text primary key,
  race_date date not null,
  jcd text not null,
  venue text,
  race_no smallint not null,
  deadline text,
  pick_rank smallint not null,
  model_version text not null,
  -- 展示前モデルの予想
  hit_probability real not null,
  favorite_lane smallint not null,
  favorite_name text,
  favorite_prob real,
  p_first real[] not null,              -- 1〜6号艇の1着確率（配列の1番目が1号艇）
  tickets jsonb not null,               -- [{combo, group, prob, stake}]（自動固定と同じ買い目・資金配分）
  picked_at timestamptz not null default now(),
  -- 展示後の予想（prediction_snapshots から。トリガーが書く）
  post_hit_probability real,
  post_favorite_lane smallint,
  post_tickets jsonb,
  post_model_version text,
  post_saved_at text,
  post_pre_exhibition boolean,          -- 展示データがそろう前に保存された予想か
  post_flag boolean,                    -- 展示後に評価が大きく変わった
  post_flag_reasons text[],
  -- 結果（pre_picks.py。中止・不成立は result_status = 'void'）
  result_status text check (result_status in ('settled', 'void')),
  trifecta_actual text,
  trifecta_payout integer,
  pre_hit boolean,
  pre_stake integer,
  pre_return integer,
  post_hit boolean,
  post_stake integer,
  post_return integer,
  result_at timestamptz
);

create index if not exists pre_exhibition_picks_race_date_idx
  on public.pre_exhibition_picks (race_date);

create table if not exists public.pre_exhibition_runs (
  race_date date primary key,
  model_version text not null,
  state_as_of date,
  race_count integer,
  candidate_count integer,
  racelist_failed integer,
  picked_count integer,
  hit_probability_by_race jsonb,       -- 候補の全レースの的中確率（選び方の見直し用）
  settings jsonb,
  code_sha text,
  picked_at timestamptz
);

alter table public.pre_exhibition_picks enable row level security;
alter table public.pre_exhibition_runs enable row level security;

drop policy if exists pre_exhibition_picks_read on public.pre_exhibition_picks;
create policy pre_exhibition_picks_read on public.pre_exhibition_picks
  for select to anon, authenticated using (true);

drop policy if exists pre_exhibition_runs_read on public.pre_exhibition_runs;
create policy pre_exhibition_runs_read on public.pre_exhibition_runs
  for select to anon, authenticated using (true);

-- そのレースの最新の予想（prediction_snapshots）を展示後の値として書く。
-- 今の新モデル（ml-chain）は補正なしの的中確率、今のモデルに戻ったときは補正後の的中確率で比べる。
create or replace function public.pre_pick_apply_post(p_race_key text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  pk public.pre_exhibition_picks%rowtype;
  s record;
  p jsonb;
  v_mv text;
  v_hp real;
  v_fav smallint;
  v_t jsonb;
  reasons text[] := '{}';
begin
  select * into pk from public.pre_exhibition_picks where race_key = p_race_key;
  if not found then
    return;
  end if;
  select * into s from public.prediction_snapshots where race_key = p_race_key
    order by saved_at desc limit 1;
  if not found then
    return;
  end if;
  p := s.payload_json::jsonb;
  v_mv := coalesce(p ->> 'model_version', '');
  v_hp := case when v_mv like 'ml-chain%' then (p ->> 'hit_probability')::real
               else coalesce((p ->> 'hit_probability_calibrated')::real, (p ->> 'hit_probability')::real) end;
  select (e ->> 'lane')::numeric::smallint into v_fav
    from jsonb_array_elements(p -> 'final') e
    order by (e ->> 'p_first')::float8 desc nulls last limit 1;
  select jsonb_agg(jsonb_build_object(
           'combo', t ->> 'combo', 'group', t ->> 'group',
           'prob', (t ->> 'prob')::float8, 'stake', coalesce((t ->> 'stake')::numeric, 0)::int))
    into v_t
    from jsonb_array_elements(p -> 'tickets') t;
  if v_fav is distinct from pk.favorite_lane then
    reasons := reasons || format('本命 %s→%s号艇', pk.favorite_lane, v_fav);
  end if;
  if v_hp is not null and v_hp <= pk.hit_probability - 0.10 then
    reasons := reasons || format('的中確率 %s%%→%s%%', round(pk.hit_probability * 100), round(v_hp * 100));
  end if;
  update public.pre_exhibition_picks set
    post_hit_probability = v_hp,
    post_favorite_lane = v_fav,
    post_tickets = v_t,
    post_model_version = nullif(v_mv, ''),
    post_saved_at = s.saved_at,
    post_pre_exhibition = s.pre_exhibition,
    post_flag = cardinality(reasons) > 0,
    post_flag_reasons = reasons
  where race_key = p_race_key;
end;
$$;

-- 選び直したとき・結果の記録の前に、その日の厳選レースへまとめて書く
create or replace function public.pre_picks_sync_post(p_race_date date)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  rk text;
begin
  for rk in select race_key from public.pre_exhibition_picks where race_date = p_race_date loop
    perform public.pre_pick_apply_post(rk);
  end loop;
end;
$$;

-- 予想の保存は止めない（展示後の値が書けなくても警告だけ）
create or replace function public.pre_pick_post_trigger()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    perform public.pre_pick_apply_post(new.race_key);
  exception when others then
    raise warning 'pre_pick_apply_post failed for %: %', new.race_key, sqlerrm;
  end;
  return new;
end;
$$;

revoke all on function public.pre_pick_apply_post(text) from public, anon, authenticated;
revoke all on function public.pre_picks_sync_post(date) from public, anon, authenticated;
revoke all on function public.pre_pick_post_trigger() from public, anon, authenticated;
grant execute on function public.pre_picks_sync_post(date) to service_role;

drop trigger if exists prediction_snapshots_pre_pick_post on public.prediction_snapshots;
create trigger prediction_snapshots_pre_pick_post
  after insert or update on public.prediction_snapshots
  for each row execute function public.pre_pick_post_trigger();
