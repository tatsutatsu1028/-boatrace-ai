-- 万舟チャンスの予想3種比較（manshu_source.py）と、事後予想の3艇BOXの仮想成績。
--
--   manshu_source_compare : 1レース3行（source = model / claude / blend）。展示後の自動予想（auto_post_predict.py）が
--                           万舟チャンスのレースで買い目を作り直すのと同じタイミングで書く
--     tri_probs   : その予想の3連単の全120通りの確率 {組番: 確率}
--                   model = モデルのみ予想 / claude = Claudeのみ予想（挙げなかった組はモデルの比率で残りを配る）/
--                   blend = 両方の予想平均（アプリの3つのボタンと同じ処理）
--     aim_tickets : 3種共通のルールの買い目 [{combo, prob, odds, tier, stake, payout}]
--                   1号艇の1着を外し、3連単オッズ30倍以上から確率の高い順に10点（各100円）。aim_method に作り方の名前
--     odds_at     : 買い目を作ったオッズを取った時刻 / deadline_at : 締切 / saved_at : 書いた時刻
--     結果（result_status・trifecta_actual・trifecta_payout・hit・stake・return）は、既存の結果確定（list_results.py）が
--     万舟チャンスの結果と一緒に書く。取りこぼしは翌朝の pre_picks.py
--     claude_model : 使った Claude のモデル / error : 作れなかった理由（上限を超えて Claude をスキップした、など）
--   manshu_source_summary : source 別のレース数・的中数・的中率・回収率（100円買い換算）。
--                           saved_at が締切より前の行だけ数える（締切後に作り直したものは入れない）。
--                           *_all3 は3種ともそろったレースだけの数字（同じレースどうしで比べる）
--   app_settings.manshu_compare_claude_daily_usd : この比較で Claude に使う1日（日本時間）の上限（ドル）。
--                           claude_usage（purpose = manshu_compare）の今日の合計が上限以上なら Claude を呼ばない。0 で止める
--
--   hindcast_box3 / hindcast_box3_summary : 事後予想（hindcast_predictions）で本命の1着確率が50〜70%のレースの、
--                           1着確率の上位3艇の3連単BOX（6点・各100円）の仮想成績
--
-- 書くのは GitHub Actions（service_role）だけ。アプリからは読まない（RLS で閉じる。ビューは security_invoker）。

create table if not exists public.manshu_source_compare (
  race_key text not null,
  race_date date not null,
  source text not null check (source in ('model', 'claude', 'blend')),
  tri_probs jsonb,
  aim_tickets jsonb not null default '[]'::jsonb,
  aim_method text,
  odds_at timestamptz,
  deadline_at timestamptz,
  saved_at timestamptz not null default now(),
  result_status text check (result_status in ('settled', 'void')),
  trifecta_actual text,
  trifecta_payout integer,
  hit boolean,
  stake integer,
  "return" integer,
  result_at timestamptz,
  claude_model text,
  error text,
  primary key (race_key, source)
);

create index if not exists manshu_source_compare_race_date_idx on public.manshu_source_compare (race_date);

alter table public.manshu_source_compare enable row level security;
revoke all on public.manshu_source_compare from anon, authenticated;

alter table public.app_settings
  add column if not exists manshu_compare_claude_daily_usd numeric(10, 2) not null default 3.00
    check (manshu_compare_claude_daily_usd >= 0);

create or replace view public.manshu_source_summary
with (security_invoker = true) as
with c as (
  select *,
         result_status = 'settled' and deadline_at is not null and saved_at < deadline_at
           and coalesce(stake, 0) > 0 as counted
  from public.manshu_source_compare
), a as (
  select c.*, count(*) filter (where counted) over (partition by race_key) = 3 as all3
  from c
)
select
  source,
  count(*) filter (where counted) as races,
  count(*) filter (where counted and hit) as hits,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where counted), 4) as hit_rate,
  sum(stake) filter (where counted) as stake,
  sum("return") filter (where counted) as "return",
  round(sum("return") filter (where counted)::numeric / nullif(sum(stake) filter (where counted), 0), 4) as roi,
  count(*) filter (where counted and all3) as races_all3,
  count(*) filter (where counted and all3 and hit) as hits_all3,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where counted and all3), 4) as hit_rate_all3,
  round(sum("return") filter (where counted and all3)::numeric
        / nullif(sum(stake) filter (where counted and all3), 0), 4) as roi_all3,
  count(*) filter (where result_at is null and coalesce(stake, 0) > 0) as pending,
  count(*) filter (where error is not null) as errors,
  count(*) filter (where deadline_at is not null and saved_at >= deadline_at) as after_deadline,
  min(race_date) as first_date,
  max(race_date) as last_date
from a
group by source;

revoke all on public.manshu_source_summary from anon, authenticated;

-- 事後予想の3艇BOX（本命の1着確率50〜70%のレース）
create or replace view public.hindcast_box3
with (security_invoker = true) as
with p as (
  select h.race_key, h.model_version, h.race_date, h.jcd, h.race_no, h.favorite_lane, h.favorite_prob,
         h.trifecta_actual, h.trifecta_payout,
         array(select u.ord::int
               from unnest(h.p_first) with ordinality as u(prob, ord)
               order by u.prob desc, u.ord
               limit 3) as box_lanes
  from public.hindcast_predictions h
  where h.favorite_prob >= 0.5 and h.favorite_prob <= 0.7
), r as (
  select p.*,
         case when p.trifecta_actual ~ '^[1-6]-[1-6]-[1-6]$' then
           split_part(p.trifecta_actual, '-', 1)::int = any(p.box_lanes)
           and split_part(p.trifecta_actual, '-', 2)::int = any(p.box_lanes)
           and split_part(p.trifecta_actual, '-', 3)::int = any(p.box_lanes)
         end as hit
  from p
)
select r.*,
       6 as points,
       case when r.hit is not null then 600 end as stake,
       case when r.hit then r.trifecta_payout when r.hit is not null then 0 end as "return"
from r;

revoke all on public.hindcast_box3 from anon, authenticated;

create or replace view public.hindcast_box3_summary
with (security_invoker = true) as
select
  model_version,
  count(*) filter (where hit is not null) as races,
  count(*) filter (where hit) as hits,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where hit is not null), 4) as hit_rate,
  sum(stake) as stake,
  sum("return") as "return",
  round(sum("return")::numeric / nullif(sum(stake), 0), 4) as roi,
  min(race_date) as first_date,
  max(race_date) as last_date
from public.hindcast_box3
group by model_version;

revoke all on public.hindcast_box3_summary from anon, authenticated;
