-- 「💥 万舟チャンス」の中穴〜万舟狙いの買い目を、確定オッズで「朝版」と「展示後版」に分けて作り、比べる。
--
--   hindcast_predictions.tri_probs : 事後予想（hindcast.py --model ml。展示・直前情報も使う）の3連単の全120通りの確率
--                                    {組番: 確率}。万舟チャンスの印のレースだけ入れる（ほかは空）
--   manshu_aim_compare             : 印のレース1行。レース後に公式サイトから取った確定3連単オッズと、
--                                    朝版（朝の3連単確率 tri_probs × 確定オッズ）・展示後版（事後予想の3連単確率 × 確定オッズ）
--                                    の買い目（今と同じ作り方: 1号艇1着を外す・中穴5点＋万舟5点・確率順。各100円）と結果
--
-- 書くのは GitHub Actions（service_role。hindcast.yml の事後予想の後に manshu_compare.py）だけ。
-- アプリ（Publishable key）には読み取りだけ許す（管理者の一覧の「これまでの成績」で並べる）。

alter table public.hindcast_predictions
  add column if not exists tri_probs jsonb;

create table if not exists public.manshu_aim_compare (
  race_key text primary key,
  race_date date not null,
  method text,                         -- 買い目の作り方（例: MIX5+5-prob-no1）
  final_odds jsonb,                    -- 確定3連単オッズ {組番: オッズ}
  odds_at timestamptz,                 -- 確定オッズを取った時刻
  odds_error text,                     -- 確定オッズを取れなかった理由（次回また取る）
  hindcast_model_version text,         -- 展示後版に使った事後予想の版
  trifecta_actual text,
  trifecta_payout integer,
  is_manshu boolean,
  -- 朝版（朝の3連単確率 × 確定オッズ）
  pre_tickets jsonb,                   -- [{combo, prob, odds, tier, stake, payout}]
  pre_error text,                      -- 作れなかった理由（朝の3連単確率が無い など）
  pre_hit boolean,
  pre_stake integer,
  pre_return integer,
  -- 展示後版（事後予想の3連単確率 × 確定オッズ）
  post_tickets jsonb,
  post_error text,
  post_hit boolean,
  post_stake integer,
  post_return integer,
  updated_at timestamptz not null default now()
);

create index if not exists manshu_aim_compare_race_date_idx
  on public.manshu_aim_compare (race_date);

alter table public.manshu_aim_compare enable row level security;

drop policy if exists manshu_aim_compare_read on public.manshu_aim_compare;
create policy manshu_aim_compare_read on public.manshu_aim_compare
  for select to anon, authenticated using (true);
