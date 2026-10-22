-- 検証タブの作り直し（📊 検証）で使うもの。
--
--   hindcast_model_daily       : 全レースの事後予想（hindcast_predictions）を1日・モデルごとにまとめたビュー。
--                                新モデル（ml-chain。展示前モデル ml-chain-pre は除く）と旧モデル（position）の
--                                本命的中・候補内的中・1号艇が負けたレースの候補内的中・万舟の数。
--                                paired_* は両方のモデルで予想した同じレースだけの数（新旧の比較に使う）
--   hindcast_predictions       : アプリ（Publishable key）から上のビューを読めるよう、読み取りだけ許す
--                                （書き込みは今までどおり GitHub Actions の service_role だけ）
--   prediction_results.model_version
--                              : track_odds.py（GitHub Actions の結果保存）が版を書いていなかったため、
--                                2026-10-15 以降の多くの行が空だった。固定予想に残っている版で埋める
--                                （20261015000000_add_prediction_model_switch.sql と同じ埋め方）

drop policy if exists hindcast_predictions_read on public.hindcast_predictions;
create policy hindcast_predictions_read on public.hindcast_predictions
  for select to anon, authenticated using (true);

create or replace view public.hindcast_model_daily
with (security_invoker = on) as
with p as (
  -- 1レース・1モデルにつき1行（同じレースに版が2つあれば新しい版）
  select distinct on (race_key, model)
    race_key, race_date, model, model_version, favorite_hit, candidate_hit, winner_lane, trifecta_payout
  from (
    select h.*,
      case
        when h.model_version like 'ml-chain-pre%' then null
        when h.model_version like 'ml-chain%' then 'new'
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

update public.prediction_results r
set model_version = coalesce(
  s.payload_json::jsonb ->> 'model_version',
  s.payload_json::jsonb -> 'final' -> 0 ->> 'model_version'
)
from public.prediction_snapshots s
where s.race_key = r.race_key
  and r.model_version is null;
