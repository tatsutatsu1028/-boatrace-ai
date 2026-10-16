-- 「💥 万舟チャンス（荒れやすいレース）」（manshu.py・pre_picks.py）の保存先。
--
--   manshu_chance_picks : 1レース1行（毎朝10行）。展示前版の万舟確率・本線と穴寄りの買い目、展示後版の確率と買い目、結果
--   pre_exhibition_runs : その日の全レースの万舟確率（印を付けなかったレースとも比べられるように）を足す
--
-- 書くのは GitHub Actions（service_role。毎朝の pre_picks.py）と、下のトリガー（security definer）だけ。
-- アプリ（Publishable key）には読み取りだけ許す（会場選択・レース一覧・予想画面に印を出す）。
--
-- 展示後: アプリか自動固定がそのレースの予想を prediction_snapshots に保存すると、トリガーが
-- その予想の展示後版の万舟確率（payload の prediction_model.manshu_post）・本線の買い目・穴寄りの買い目を
-- post_* に書く。結果（万舟だったか・本線と穴寄りの的中・払戻）は翌朝の pre_picks.py が書く。

create table if not exists public.manshu_chance_picks (
  race_key text primary key,
  race_date date not null,
  jcd text not null,
  venue text,
  race_no smallint not null,
  deadline text,
  pick_rank smallint not null,
  manshu_prob real not null,            -- 展示前版の万舟確率
  manshu_model_version text not null,
  model_version text not null,          -- 展示前モデル（ml-chain-pre）の版
  favorite_lane smallint,
  favorite_prob real,
  p_first real[],                       -- 1〜6号艇の1着確率（展示前モデル）
  tickets jsonb not null,               -- 本線: [{combo, group, prob, stake}]（展示前厳選10と同じ作り方・資金配分）
  longshot_tickets jsonb not null,      -- 穴寄り: [{combo, prob}]（1号艇を1着から外した上位10点。資金配分しない）
  picked_at timestamptz not null default now(),
  -- 展示後（prediction_snapshots から。トリガーが書く）
  post_manshu_prob real,
  post_manshu_model_version text,
  post_model_version text,
  post_tickets jsonb,
  post_longshot_tickets jsonb,
  post_saved_at text,
  post_pre_exhibition boolean,
  -- 結果（pre_picks.py。中止・不成立は result_status = 'void'。穴寄りは1点100円として記録）
  result_status text check (result_status in ('settled', 'void')),
  trifecta_actual text,
  trifecta_payout integer,
  is_manshu boolean,
  main_hit boolean,
  main_stake integer,
  main_return integer,
  longshot_hit boolean,
  longshot_stake integer,
  longshot_return integer,
  post_main_hit boolean,
  post_main_stake integer,
  post_main_return integer,
  post_longshot_hit boolean,
  post_longshot_stake integer,
  post_longshot_return integer,
  result_at timestamptz
);

create index if not exists manshu_chance_picks_race_date_idx
  on public.manshu_chance_picks (race_date);

alter table public.pre_exhibition_runs
  add column if not exists manshu_model_version text,
  add column if not exists manshu_picked_count integer,
  add column if not exists manshu_prob_by_race jsonb,   -- その日の全レースの展示前版の万舟確率
  add column if not exists manshu_error text;          -- 万舟チャンスだけ失敗したときの理由（厳選10は続ける）

alter table public.manshu_chance_picks enable row level security;

drop policy if exists manshu_chance_picks_read on public.manshu_chance_picks;
create policy manshu_chance_picks_read on public.manshu_chance_picks
  for select to anon, authenticated using (true);

-- そのレースの最新の予想（prediction_snapshots）から展示後の値を書く
create or replace function public.manshu_pick_apply_post(p_race_key text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  s record;
  p jsonb;
  mp jsonb;
  v_t jsonb;
  v_l jsonb;
begin
  if not exists (select 1 from public.manshu_chance_picks where race_key = p_race_key) then
    return;
  end if;
  select * into s from public.prediction_snapshots where race_key = p_race_key
    order by saved_at desc limit 1;
  if not found then
    return;
  end if;
  p := s.payload_json::jsonb;
  mp := p -> 'prediction_model' -> 'manshu_post';
  select jsonb_agg(jsonb_build_object(
           'combo', t ->> 'combo', 'group', t ->> 'group',
           'prob', (t ->> 'prob')::float8, 'stake', coalesce((t ->> 'stake')::numeric, 0)::int))
    into v_t
    from jsonb_array_elements(p -> 'tickets') t;
  if jsonb_typeof(mp -> 'longshot') = 'array' and jsonb_array_length(mp -> 'longshot') > 0 then
    v_l := mp -> 'longshot';
  end if;
  update public.manshu_chance_picks set
    post_manshu_prob = (mp ->> 'prob')::real,
    post_manshu_model_version = mp ->> 'version',
    post_model_version = nullif(coalesce(p ->> 'model_version', ''), ''),
    post_tickets = v_t,
    post_longshot_tickets = v_l,
    post_saved_at = s.saved_at,
    post_pre_exhibition = s.pre_exhibition
  where race_key = p_race_key;
end;
$$;

create or replace function public.manshu_picks_sync_post(p_race_date date)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  rk text;
begin
  for rk in select race_key from public.manshu_chance_picks where race_date = p_race_date loop
    perform public.manshu_pick_apply_post(rk);
  end loop;
end;
$$;

-- 予想の保存は止めない（展示後の値が書けなくても警告だけ）
create or replace function public.manshu_pick_post_trigger()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    perform public.manshu_pick_apply_post(new.race_key);
  exception when others then
    raise warning 'manshu_pick_apply_post failed for %: %', new.race_key, sqlerrm;
  end;
  return new;
end;
$$;

revoke all on function public.manshu_pick_apply_post(text) from public, anon, authenticated;
revoke all on function public.manshu_picks_sync_post(date) from public, anon, authenticated;
revoke all on function public.manshu_pick_post_trigger() from public, anon, authenticated;
grant execute on function public.manshu_picks_sync_post(date) to service_role;

drop trigger if exists prediction_snapshots_manshu_pick_post on public.prediction_snapshots;
create trigger prediction_snapshots_manshu_pick_post
  after insert or update on public.prediction_snapshots
  for each row execute function public.manshu_pick_post_trigger();
