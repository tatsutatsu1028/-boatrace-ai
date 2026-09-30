-- pg_cron による auto_random_fix.yml（自動固定）と collect_history_workflow.yml（日次収集）の起動。
--
-- GitHub Actions の schedule トリガーは数時間単位の遅延・欠落が続いた
-- （自動固定は 9/30 02:00 JST を最後に翌朝まで起動しなかった）ため、
-- track_odds などと同じく workflow_dispatch を直接叩いて起動する。
-- 両ワークフローの schedule トリガーは外した（二重起動を避けるため）。
--
--   trigger-auto-random-fix : JST 8:20〜20:50 の毎時20分・50分（30分おき）
--                             = UTC 23:20〜11:50（UTC日付をまたぐので時の欄は 23,0-11）
--   trigger-collect-history : 毎日 JST 00:32（UTC 15:32）に前日分（inputs.daily=true）
--
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ
-- （vault の github_track_odds_token を使う）。collect_history の inputs.daily は
-- main の collect_history_workflow.yml に定義されてから適用すること（未定義だと422）。
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-auto-random-fix',
  '20,50 23,0-11 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/auto_random_fix.yml/dispatches',
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

select cron.schedule(
  'trigger-collect-history',
  '32 15 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/collect_history_workflow.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"daily":"true","start":"20260601","end":"20260601","max_hours":"3"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
