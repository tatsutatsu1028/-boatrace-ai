-- 展示後の自動予想（auto_post_predict.py・auto_post_predict.yml）。
--
--   auto_post_log : 1レース1行。その日の展示前厳選10・万舟チャンスのレース（重複は1行）を、締切20〜10分前に
--                   予想できたか・見送ったか（理由）・何回確かめたか・万舟狙いの買い目を作り直せたか
--     status  waiting : 確かめたが展示タイムがそろっていない（次の回にもう一度）
--             done    : 予想して保存した
--             already : 展示後の予想が保存済み（手動・自動固定）なのでスキップ
--             skipped : 見送り（10分前を過ぎてもそろわない・中止・一度も確かめられなかった）
--   pg_cron       : 5分おき（JST 8:00〜22:55。ナイターの最終レースまで）に task=run、毎晩 23:10 JST に task=report
--
-- 予想の保存は手動と同じ save_latest_prediction_snapshot()（collector_name = 'auto_post'。自動固定の
-- 'auto_random' とは別なので、自動固定の1日の件数・auto_result_collect.py の結果記録には数えない）。
-- 保存すると今までのトリガーが pre_exhibition_picks / manshu_chance_picks の post_* を書く。
-- 結果は手動の予想と同じくオッズ追跡（track_odds.py）が prediction_results に1レース1行で記録する
-- （collector_name は保存した予想から写すので 'auto_post'）。
--
-- 書くのは GitHub Actions（service_role）だけ。アプリからは読まない（RLS で閉じる）。
-- 前提は 20260924233954_manage_track_odds_pg_cron_jobs.sql と同じ（vault の github_track_odds_token）。
-- 止めるとき:
--   select cron.unschedule('trigger-auto-post-predict');
--   select cron.unschedule('trigger-auto-post-report');

create table if not exists public.auto_post_log (
  race_key text primary key,
  race_date date not null,
  jcd text not null,
  venue text,
  race_no smallint not null,
  deadline text,
  sources text[] not null,               -- {pre}（展示前厳選10）/ {manshu}（万舟チャンス）/ 両方
  status text not null check (status in ('waiting', 'done', 'already', 'skipped')),
  reason text,
  attempts integer not null default 0,   -- 展示を確かめた回数
  last_tried_at timestamptz,
  done_at timestamptz,
  snapshot_status text,                  -- save_latest_prediction_snapshot の結果（inserted / updated など）
  aim_status text,                       -- 万舟狙いの買い目の作り直し（万舟チャンスだけ）
  updated_at timestamptz not null default now()
);

create index if not exists auto_post_log_race_date_idx on public.auto_post_log (race_date);

alter table public.auto_post_log enable row level security;
revoke all on public.auto_post_log from anon, authenticated;

-- 中穴〜万舟狙いの買い目の作り方に「展示後の自動予想（auto）」を足す。aim_source は text なので変更なし。

select cron.schedule(
  'trigger-auto-post-predict',
  '*/5 23,0-13 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/auto_post_predict.yml/dispatches',
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

select cron.schedule(
  'trigger-auto-post-report',
  '10 14 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/auto_post_predict.yml/dispatches',
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
  body := '{"ref":"main","inputs":{"task":"report"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
