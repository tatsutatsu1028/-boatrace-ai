-- 候補の版（ml-chain-v3。本番には使わない）を事後予想で今の版（ml-chain-v2）と並べて比べる。
--
--   hindcast_predictions.p_head_alt : 候補の版のサブモデル（1号艇が1着でなかったレースだけで学習）の
--                                     2〜6号艇の1着確率（1〜6号艇の順。1号艇と、いない艇は null）。
--                                     hindcast.py --model candidate が書く。v2 の行は null
--   hindcast_model_daily            : 検証タブのビュー。新モデル（'new'）を本番の系統 v2 だけにする
--                                     （v3 の行が「新しい版」として混ざらないように）
--   hindcast_candidate_races        : 同じレースの v2 と v3 を1行に並べ、本命の的中と
--                                     万舟チャンスの頭（1号艇以外で1着確率が高い2艇。頭1・頭2）の的中を出す
--                                       base = v2 の1着確率（今の頭の決め方）
--                                       v3   = v3 の1着確率（本命と同じ1着の段）
--                                       alt  = v3 のサブモデル（1号艇が負けたレースだけで学習）
--   hindcast_candidate_summary      : 上を「全レース／万舟チャンスの印のレース／1号艇が負けたレース」で集計
--
-- 合格基準（2026-11 の依頼）: 万舟チャンスの印のレースで頭1・頭2の的中が今（base）より上がり、
-- 全レースの本命の的中率（favorite_hit）が v2 より下がらないこと。本番の切り替えはオーナーの確認後。

alter table public.hindcast_predictions
  add column if not exists p_head_alt real[];

create or replace view public.hindcast_model_daily
with (security_invoker = on) as
with p as (
  -- 1レース・1モデルにつき1行（同じレースに版が2つあれば新しい版）
  select distinct on (race_key, model)
    race_key, race_date, model, model_version, favorite_hit, candidate_hit, winner_lane, trifecta_payout
  from (
    select h.*,
      case
        when h.model_version like 'ml-chain-v2-%' then 'new'
        when h.model_version like 'ml-chain%' then null   -- 展示前モデル・候補の版（v3）は除く
        when h.model_version like 'position%' then 'old'
      end as model
    from public.hindcast_predictions h
    where h.candidate_hit is not null
  ) x
  where model is not null
  order by race_key, model, model_version desc
), q as (
  select p.*, count(*) over (partition by race_key) = 2 as paired from p
)
select
  race_date,
  model,
  max(model_version) as model_version,
  count(*) as races,
  count(*) filter (where favorite_hit) as favorite_hits,
  count(*) filter (where candidate_hit) as candidate_hits,
  count(*) filter (where winner_lane <> 1) as lane1_lost,
  count(*) filter (where winner_lane <> 1 and candidate_hit) as lane1_lost_candidate_hits,
  count(*) filter (where trifecta_payout >= 10000) as manshu_races,
  count(*) filter (where paired) as paired_races,
  count(*) filter (where paired and favorite_hit) as paired_favorite_hits,
  count(*) filter (where paired and candidate_hit) as paired_candidate_hits,
  count(*) filter (where paired and winner_lane <> 1) as paired_lane1_lost,
  count(*) filter (where paired and winner_lane <> 1 and candidate_hit) as paired_lane1_lost_candidate_hits
from q
group by race_date, model;

grant select on public.hindcast_model_daily to anon, authenticated;

-- 1〜6号艇の確率の配列から、2〜6号艇で確率が高い上位2艇（同じ確率なら内側）
create or replace function public.top2_non_lane1(p real[])
returns int[] language sql immutable as $$
  select coalesce(array(
    select u.ord::int
    from unnest(p) with ordinality as u(prob, ord)
    where u.ord > 1 and u.prob is not null
    order by u.prob desc, u.ord
    limit 2), '{}'::int[])
$$;

create or replace view public.hindcast_candidate_races
with (security_invoker = true) as
with v2 as (
  select distinct on (race_key) *
  from public.hindcast_predictions
  where model_version like 'ml-chain-v2-%' and winner_lane is not null
  order by race_key, model_version desc
), v3 as (
  select distinct on (race_key) *
  from public.hindcast_predictions
  where model_version like 'ml-chain-v3-%' and winner_lane is not null
  order by race_key, model_version desc
), j as (
  select a.race_key, a.race_date, a.jcd, a.race_no, a.winner_lane, a.trifecta_payout,
         a.model_version as v2_version, b.model_version as v3_version,
         a.favorite_lane as v2_favorite, b.favorite_lane as v3_favorite,
         a.favorite_hit as v2_favorite_hit, b.favorite_hit as v3_favorite_hit,
         public.top2_non_lane1(a.p_first) as base_heads,
         public.top2_non_lane1(b.p_first) as v3_heads,
         public.top2_non_lane1(b.p_head_alt) as alt_heads,
         exists (select 1 from public.manshu_chance_picks m where m.race_key = a.race_key) as manshu_mark
  from v2 a join v3 b using (race_key)
)
select j.*,
       j.winner_lane = j.base_heads[1] as base_head1_hit,
       j.winner_lane = any(j.base_heads) as base_head2_hit,
       j.winner_lane = j.v3_heads[1] as v3_head1_hit,
       j.winner_lane = any(j.v3_heads) as v3_head2_hit,
       case when cardinality(j.alt_heads) > 0 then j.winner_lane = j.alt_heads[1] end as alt_head1_hit,
       case when cardinality(j.alt_heads) > 0 then j.winner_lane = any(j.alt_heads) end as alt_head2_hit
from j;

revoke all on public.hindcast_candidate_races from anon, authenticated;

create or replace view public.hindcast_candidate_summary
with (security_invoker = true) as
with s as (
  select '1 全レース' as scope, r.* from public.hindcast_candidate_races r
  union all
  select '2 万舟チャンスの印', r.* from public.hindcast_candidate_races r where r.manshu_mark
  union all
  select '3 1号艇が負けたレース', r.* from public.hindcast_candidate_races r where r.winner_lane <> 1
  union all
  select '4 万舟チャンスの印で1号艇が負けた', r.* from public.hindcast_candidate_races r
  where r.manshu_mark and r.winner_lane <> 1
)
select scope,
       count(*) as races,
       min(race_date) as first_date,
       max(race_date) as last_date,
       round(avg(v2_favorite_hit::int), 4) as v2_favorite_rate,
       round(avg(v3_favorite_hit::int), 4) as v3_favorite_rate,
       round(avg(base_head1_hit::int), 4) as base_head1_rate,
       round(avg(v3_head1_hit::int), 4) as v3_head1_rate,
       round(avg(alt_head1_hit::int), 4) as alt_head1_rate,
       round(avg(base_head2_hit::int), 4) as base_head2_rate,
       round(avg(v3_head2_hit::int), 4) as v3_head2_rate,
       round(avg(alt_head2_hit::int), 4) as alt_head2_rate,
       count(*) filter (where alt_head2_hit and not base_head2_hit) as alt_only_head2,
       count(*) filter (where base_head2_hit and not alt_head2_hit) as base_only_head2
from s
group by scope
order by scope;

revoke all on public.hindcast_candidate_summary from anon, authenticated;
