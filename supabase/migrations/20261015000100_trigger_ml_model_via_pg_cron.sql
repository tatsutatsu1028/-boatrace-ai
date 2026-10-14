-- pg_cron による ml_model.yml（新しい予想モデルの定期処理）の起動。本番の予想を新しいモデルに
-- 切り替えたので、アプリと自動固定が読む集計表と学習済みモデルを定期的に作り直す。
--
--   trigger-ml-state : 毎日 JST 05:05（UTC 20:05）  task=state 前日までの集計表 models/ml_state.pkl.gz
--                      公式サイトのメンテナンス（04:00〜04:30）の後、自動固定（08:20〜）の前。
--                      アプリは1時間ごとにデータ用リポジトリを確かめるので、開催前に新しい集計表に替わる
--   trigger-ml-train : 毎週月曜 JST 06:10（UTC 日曜 21:10） task=train 2年分で学習し直し。直近28日で
--                      前の版より悪ければ latest.json を変えない（ml_nightly.train）
--
-- 集計表が3日より古い・読めないときは、アプリと自動固定は今のモデルで予想する（ml_live.production_final）。
-- 前提は 20261006000100_trigger_hindcast_via_pg_cron.sql と同じ（vault の github_track_odds_token）。
-- 止めるときは select cron.unschedule('trigger-ml-state'); select cron.unschedule('trigger-ml-train');
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-ml-state',
  '5 20 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/ml_model.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"task":"state"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);

select cron.schedule(
  'trigger-ml-train',
  '10 21 * * 0',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/ml_model.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"task":"train"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
