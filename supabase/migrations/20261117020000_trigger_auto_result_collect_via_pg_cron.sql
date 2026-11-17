-- pg_cron による auto_result_collect.yml（結果の確定・一覧の結果の書き込み）の定期起動。
--
-- auto_result_collect.yml は GitHub の schedule（15分おき）だけで動かしていたが、発火の間引きが常態化し、
-- 2026-11-17 は JST 8:01 の1回のあと 11時台まで一度も動かなかった。展示後の予想をしていない一覧のレース
-- （list_results.sync_pending が拾う）の結果がその日のうちに書かれず、結果の通知も出なかった（江戸川1R）。
-- refresh_schedule_status.yml・track_odds.yml と同じく、workflow_dispatch を直接叩いて起動する。
-- workflow 側の schedule は保険として残す（重複して動いても、結果の無い行だけを書くので結果は同じ）。
--
-- JST 8:22〜22:52 の15分おき（UTC 23時・0〜13時台の7分・22分・37分・52分）。
-- 毎時0分ちょうどは他の起動と重ならないようずらす。
-- 前提は 20260926010500_trigger_refresh_schedule_status_via_pg_cron.sql と同じ（vault の github_track_odds_token）。
-- やめるときは select cron.unschedule('trigger-auto-result-collect');
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-auto-result-collect',
  '7,22,37,52 23,0-13 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/auto_result_collect.yml/dispatches',
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
