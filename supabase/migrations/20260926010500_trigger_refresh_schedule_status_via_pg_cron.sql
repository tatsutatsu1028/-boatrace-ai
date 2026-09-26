-- pg_cronによるrefresh_schedule_status.ymlの定期起動。
--
-- track_odds.ymlと同様、GitHub Actionsのscheduleトリガーでは発火の遅延・欠落が
-- 常態化しており、refresh_schedule_status.ymlは1日30回の予定に対して実際には
-- 1日3〜4回しか発火していなかった（9/26はJST 0:31の発火を最後に、JST 10時過ぎ
-- まで一度も発火せず、締切を過ぎた会場も「発売開始前」のまま残った）。
-- そのため track_odds.yml と同じ仕組みで workflow_dispatch を直接叩いて起動する。
-- workflow側のscheduleトリガーは保険として残す（重複実行しても結果は同じ）。
--
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ
-- （pg_cron / pg_net / supabase_vault、Vaultの github_track_odds_token）。
-- トークンはリポジトリ単位の Actions: write 権限なので、このworkflowにも使える。
--
-- cron.scheduleは同名ジョブがあれば上書きするため、再実行しても重複しない。

-- JST 8:05〜22:35 の30分おき（UTC 23時・0〜13時台の5分・35分）。
-- workflow側のscheduleと同じ時刻に合わせる。
select cron.schedule(
  'trigger-refresh-schedule-status',
  '5,35 23,0-13 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/refresh_schedule_status.yml/dispatches',
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
