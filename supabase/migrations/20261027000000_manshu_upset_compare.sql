-- 「1号艇が負けた場合の着順」専用モデル（ml_upset.py）の検証用（本番の予想・買い目・画面は変えない）。
--
--   manshu_upset_compare : 「💥 万舟チャンス」の印のレース1行。今のモデルの3連単確率の1号艇1着の組はそのまま、
--                          それ以外を（1 − 1号艇の1着確率）× 専用モデルに置き換えた3連単の全120通りの確率と、
--                          確定3連単オッズ（manshu_aim_compare.final_odds）で今のモデル・専用モデルの両方から作った
--                          作り方A（1号艇1着を外す・中穴5点＋万舟5点・確率順）・E（1着2艇・確率順10点）の買い目と結果
--
-- 書くのは GitHub Actions（service_role。hindcast.yml の manshu_compare.py の後に upset_compare.py）だけ。
-- アプリ（Publishable key）には読み取りだけ許す（検証タブで並べる）。

create table if not exists public.manshu_upset_compare (
  race_key text primary key,
  race_date date not null,
  trifecta_actual text,
  trifecta_payout integer,
  is_manshu boolean,
  winner_lane integer,
  -- 展示後版（事後予想の3連単確率 hindcast_predictions.tri_probs を専用モデルの展示後の版で置き換え）
  post_version text,                   -- 専用モデルの版
  post_base_version text,              -- 1号艇の1着確率に使った事後予想の版
  post_tri_probs jsonb,                -- {組番: 確率}（120通り。合計1）
  post_summary jsonb,                  -- {first: {艇番: 1号艇が負けたときの1着確率}, top1, top1_prob, top2, top2_prob, p_lane1_lose}
  post_error text,                     -- 作れなかった理由
  -- 朝版（朝の3連単確率 manshu_chance_picks.tri_probs を専用モデルの展示前の版で置き換え）
  pre_version text,
  pre_tri_probs jsonb,
  pre_summary jsonb,
  pre_error text,
  -- 確定オッズでの買い目の比較 {pre|post: {current|upset: {top2, top1_hit, top2_hit, rank_actual,
  --   A: {hit, stake, return, prob_sum}, E: {...}}}}
  variants jsonb,
  variant_tickets jsonb,               -- {pre|post: {current|upset: {A: [買い目], E: [買い目]}}}
  odds_ready boolean,                  -- 確定オッズがあったか（無ければ買い目は次回作る）
  updated_at timestamptz not null default now()
);

create index if not exists manshu_upset_compare_race_date_idx
  on public.manshu_upset_compare (race_date);

alter table public.manshu_upset_compare enable row level security;

drop policy if exists manshu_upset_compare_read on public.manshu_upset_compare;
create policy manshu_upset_compare_read on public.manshu_upset_compare
  for select to anon, authenticated using (true);
