-- pg_cronによる history_backfill.yml（モデル作り直し用の過去データ収集）の夜間起動。
--
-- GitHub Actionsのscheduleトリガーは遅延・欠落が多いため、他のワークフローと
-- 同じく workflow_dispatch を直接叩いて起動する（このワークフローには
-- scheduleトリガー自体を置いていない）。
--
-- 1晩に2回:
--   22:40 JST (13:40 UTC) 起動 → 03:50 JST に停止（公式サイトのメンテナンス 4:00〜4:30 の前）
--   04:40 JST (19:40 UTC) 起動 → 08:00 JST に停止（朝の開催・事前取得の前）
-- 収集範囲は 2024-09-27〜昨日。新しい日付から順に取り、取得済みは飛ばす。
-- 全部そろった後も、毎晩その日の分を足していく（数分で終わる）。
--
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ
-- （vault の github_track_odds_token を使う）。workflow の inputs（stop_at/start/mode）が
-- mainの history_backfill.yml に定義されてから適用すること（未定義だと422）。
-- cron.scheduleは同名ジョブがあれば上書きするため、再実行しても重複しない。

select cron.schedule(
  'trigger-history-backfill-night',
  '40 13 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/history_backfill.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"stop_at":"03:50","start":"20240927","mode":"all"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);

select cron.schedule(
  'trigger-history-backfill-morning',
  '40 19 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/history_backfill.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"stop_at":"08:00","start":"20240927","mode":"pages"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
