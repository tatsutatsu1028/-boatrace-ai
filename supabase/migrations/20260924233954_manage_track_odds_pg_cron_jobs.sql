-- pg_cronによるtrack_odds.ymlの定期起動と、実行ログの定期削除。
--
-- GitHub Actionsのscheduleトリガーは発火の遅延・欠落が多く（実測で1日数回
-- しか発火しない日もある）、オッズ追跡が止まってしまうため、2026-08-21に
-- SQL Editorから手作業で以下のジョブを登録していた。これまでリポジトリに
-- 記録がなかったため、現在の定義をこのマイグレーションで管理する。
--
-- 前提（このマイグレーションでは作成しない。トークンをリポジトリに置かないため）:
--   - 拡張機能 pg_cron / pg_net / supabase_vault が有効であること
--   - Vaultに name = 'github_track_odds_token' のシークレットがあること
--     （tatsutatsu1028/-boatrace-ai の Actions: write 権限を持つGitHubトークン）
--       select vault.create_secret('<token>', 'github_track_odds_token',
--                                  'GitHub Actions Track Odds trigger token');
--     トークン更新時は vault.update_secret(<id>, '<new token>') を使う。
--
-- cron.scheduleは同名ジョブがあれば上書きするため、再実行しても重複しない。

-- 5分おきにtrack_odds.ymlをworkflow_dispatchで起動する。
-- pg_cronはUTC基準。以前は24時間動いていたが、track_odds.ymlの想定稼働時間
-- （JST 8:00〜22:55）に合わせてUTC 23時・0〜13時台に絞る。
select cron.schedule(
  'trigger-track-odds-every-5-minutes',
  '*/5 23,0-13 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/track_odds.yml/dispatches',
  headers := jsonb_build_object(
    'Accept', 'application/vnd.github+json',
    'Authorization', 'Bearer ' || (
      select decrypted_secret
      from vault.decrypted_secrets
      where name = 'github_track_odds_token'
    ),
    'X-GitHub-Api-Version', '2026-03-10',
    'Content-Type', 'application/json'
  ),
  body := '{"ref":"main"}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);

-- cron.job_run_detailsは自動では削除されず、上のジョブだけで1日約200行ずつ
-- 増え続けるため、7日より前の実行ログを毎日UTC 18:15（JST 3:15）に削除する。
select cron.schedule(
  'purge-cron-job-run-details',
  '15 18 * * *',
  $cmd$delete from cron.job_run_details where end_time < now() - interval '7 days'$cmd$
);
