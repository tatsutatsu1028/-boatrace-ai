-- threads_config（スレッズのアクセストークン）を外から読めないようにする。
--
-- 読み書きするのは次の2つだけ:
--   - アプリのスレッズ連携・投稿画面: Streamlit Secrets の [supabase] service_key
--     （Secret key、sb_secret_…）で読み書きする（result_tracker.supabase_service_config）
--   - track_odds.py のトークン自動更新: GitHub Actions の SUPABASE_SERVICE_ROLE_KEY
-- どちらも service_role で RLS を通らないので、ポリシーを付けずに RLS を有効にする。
-- これで Publishable key（anon）や authenticated からは読み書きできなくなる。
--
-- 適用の順番: アプリの変更を main に入れ、Streamlit Secrets に service_key を
-- 追加した後に適用する（先に適用すると、アプリのスレッズ投稿が「未設定」になる）。

alter table public.threads_config enable row level security;
