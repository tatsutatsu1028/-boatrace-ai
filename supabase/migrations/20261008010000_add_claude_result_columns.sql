-- ＋Claude予想: 結果確定後に「モデルの買い目」と「モデル＋Claudeの買い目」のどちらが当たったかを残す列。
-- Claude の読みがある予想（管理者が「AI最終予想」を押したレース）だけ値が入り、それ以外は空。
-- モデルの買い目の当たり外れは既存の列（candidate_hit・hit_any_ticket・payout・total_stake）。
-- 予想の中身（Claude の1着確率・理由・両方の買い目）は prediction_snapshots.payload_json の "claude" にある。

alter table public.prediction_results
  add column if not exists claude_model text,
  add column if not exists claude_p1_lane integer,
  add column if not exists claude_first_hit boolean,
  add column if not exists mix_p1_lane integer,
  add column if not exists mix_first_hit boolean,
  add column if not exists mix_candidate_count integer,
  add column if not exists mix_candidate_hit boolean,
  add column if not exists mix_hit_any_ticket boolean,
  add column if not exists mix_total_stake integer,
  add column if not exists mix_payout integer;

comment on column public.prediction_results.claude_first_hit is 'Claude の1着確率が最も高い艇が1着だったか';
comment on column public.prediction_results.mix_first_hit is 'モデルとClaudeの平均の1着確率が最も高い艇が1着だったか';
comment on column public.prediction_results.mix_candidate_hit is 'モデル＋Claudeの買い目候補に実際の3連単が入っていたか';
comment on column public.prediction_results.mix_hit_any_ticket is 'モデル＋Claudeの買い目（金額が付いたもの）が当たったか';
comment on column public.prediction_results.mix_payout is 'モデル＋Claudeの買い目の払戻（円）';
