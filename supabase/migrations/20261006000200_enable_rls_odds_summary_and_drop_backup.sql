-- odds_summary の RLS 有効化と、不要になったバックアップの削除。
--
-- odds_summary は track_odds.py（odds_rollup.py）が GitHub Actions から
-- service_role（Secret key）で書くだけで、アプリ（Publishable key = anon）は
-- 読み書きしていない（edge logs でも anon からのアクセスは無し）。
-- service_role は RLS を通らないので、ポリシーを付けずに RLS を有効にすれば
-- 自動処理はそのまま動き、anon / authenticated からは読み書きできなくなる。
--
-- odds_summary を元にしたビュー odds_drift_by_group / odds_missed_hits は
-- 作成者の権限で動くため、そのままだと RLS を素通りして anon から読める。
-- security_invoker にして、呼び出した側の権限（RLS）で読むようにする。
-- （どちらもアプリでは使っていない。SQLエディタや service_role からは従来どおり読める。）

alter table public.odds_summary enable row level security;

alter view public.odds_drift_by_group set (security_invoker = true);
alter view public.odds_missed_hits set (security_invoker = true);

-- 2026-09-23 に取った prediction_results の買い目のバックアップ。もう不要。
-- （削除までの間も外から読めないよう、先に RLS を有効にしてある。）
drop table if exists public.prediction_results_tickets_backup_20260923;
