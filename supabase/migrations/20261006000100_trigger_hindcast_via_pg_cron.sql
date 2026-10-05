-- pg_cron による hindcast.yml（全レースの事後予想）の起動。
--
--   trigger-hindcast : 毎日 JST 01:40（UTC 16:40）
--
-- 00:32 JST の trigger-collect-history（日次収集、実績で約15分）が history_full.csv に
-- 前日分を足した後に動かす。hindcast.py は「2026-08-18〜昨日のうち未保存の日」を
-- 処理するので、初回は過去分をまとめて作り、遅れた日も次回に自動で埋まる。
--
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ
-- （vault の github_track_odds_token を使う）。hindcast.yml が main に入るまでは
-- 起動要求が404になるだけで害はない（入った晩から動く）。
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-hindcast',
  '40 16 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/hindcast.yml/dispatches',
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
