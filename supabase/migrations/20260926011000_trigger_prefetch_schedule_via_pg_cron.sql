-- pg_cronによるprefetch_schedule.ymlの定期起動。
--
-- refresh_schedule_status.yml / track_odds.yml と同じく、GitHub Actionsの
-- scheduleトリガーの遅延・欠落対策として workflow_dispatch を直接叩いて起動する。
-- workflow側のscheduleトリガーは保険として残す。
--
-- 注意: prefetch_schedule.yml は workflow_dispatch だと既定で全量取得(full)に
-- なるため、inputs.mode = 'ensure' を渡して「当日分が埋まっていれば何もしない」
-- 動作にする。このinputはmainのprefetch_schedule.ymlに定義されている必要があり
-- （未定義だとGitHubが422 Unexpected inputsを返す）、workflowの変更がmainに
-- 入った後にこのマイグレーションを適用すること。
--
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ。
-- cron.scheduleは同名ジョブがあれば上書きするため、再実行しても重複しない。

-- JST 6:05〜8:35 の30分おき（UTC 21〜23時台の5分・35分）。
-- workflow側のscheduleと同じ時刻に合わせる。
select cron.schedule(
  'trigger-prefetch-schedule',
  '5,35 21-23 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/prefetch_schedule.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"mode":"ensure"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
