-- 3つの予想（モデルのみ／Claudeのみ／両方の予想平均）の当たり外れ。
--   モデルのみ   … 既存の列（candidate_hit・hit_any_ticket・total_stake・payout）
--   Claudeのみ   … この列（claude_*）
--   両方の予想平均 … 20261008010000 で足した mix_* の列
-- 値が入るのは、管理者が「Claudeのみ予想」か「両方の予想平均」で Claude の予想を取ったレースだけ。

alter table public.prediction_results
  add column if not exists claude_candidate_count integer,
  add column if not exists claude_candidate_hit boolean,
  add column if not exists claude_hit_any_ticket boolean,
  add column if not exists claude_total_stake integer,
  add column if not exists claude_payout integer;

comment on column public.prediction_results.claude_hit_any_ticket is 'Claudeのみ予想の買い目（金額が付いたもの）が当たったか';
comment on column public.prediction_results.claude_payout is 'Claudeのみ予想の買い目の払戻（円）';
comment on column public.prediction_results.mix_hit_any_ticket is '両方の予想平均の買い目（金額が付いたもの）が当たったか';
comment on column public.prediction_results.mix_payout is '両方の予想平均の買い目の払戻（円）';
