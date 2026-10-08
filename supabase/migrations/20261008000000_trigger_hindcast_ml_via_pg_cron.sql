-- pg_cron による hindcast.yml の新しい予想モデル（--model ml）での起動。
-- 今のモデルと新しいモデルを hindcast_predictions に model_version で分けて並べ、1週間比べる。
--
--   trigger-hindcast-ml : 毎日 JST 04:10（UTC 19:10）
--
-- 新しいモデルの特徴量には出走表・直前情報（data/history/pages）が要る。History Backfill の夜の回
-- （22:40 JST〜）が前日分を最初に取るので、04:10 には2日前の分までそろっている。hindcast.py は
-- 「未保存で、出走表・直前情報がそろい、その日より前に学習した版がある日」だけを作るので、
-- 1日遅れで毎晩1日ずつ埋まる（start は最初の版の学習の翌日）。
--
-- 前提は 20261006000100_trigger_hindcast_via_pg_cron.sql と同じ（vault の github_track_odds_token）。
-- 検証をやめるときは select cron.unschedule('trigger-hindcast-ml');
-- cron.schedule は同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-hindcast-ml',
  '10 19 * * *',
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
  body := '{"ref":"main","inputs":{"model":"ml","start":"20261007"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
