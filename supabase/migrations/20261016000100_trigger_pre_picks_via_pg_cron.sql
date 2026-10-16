-- pg_cron による pre_picks.yml（「🌅 展示前厳選10」の毎朝の選定と、前の日までの結果の記録）の起動。
--
--   trigger-pre-picks : 毎日 JST 08:05（UTC 23:05）
--
-- 朝の過去データ収集（trigger-history-backfill-morning。04:40〜08:00 JST）と重ならないよう 08:05 に動かす。
-- それでも収集が動いていれば、pre_picks.py が終わるのを待ってから公式サイトを読む（最大30分）。
-- 集計表（trigger-ml-state 05:05）はこの時点で当日用になっている。
-- 公式サイトへは番組表1つと各レースの出走表1ページずつ（約150件・開始間隔3.4秒以上で約9分）。
--
-- 前提は 20261006000100_trigger_hindcast_via_pg_cron.sql と同じ（vault の github_track_odds_token）。
-- 止めるときは select cron.unschedule('trigger-pre-picks');
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-pre-picks',
  '5 23 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/pre_picks.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"task":"run"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
