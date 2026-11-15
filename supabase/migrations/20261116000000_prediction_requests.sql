-- 予想ボタンの計算を GitHub Actions（predict_race.yml → remote_predict.py）で行うための依頼の記録。
--
--   prediction_requests : 予想ボタン1回 = 1行。
--     status  queued  : アプリが依頼し、Actions を起動した
--             running : Actions が計算を始めた
--             done    : 予想・保存まで済んだ（result に表示用の結果、save_status に保存の結果）
--             error   : 失敗（error に理由）
--     input  : アプリが送る、予想に使う表（出走表・展示＝work、3連単オッズ）と設定（アプリで計算するときと同じ入力。
--              画面で直した値もそのまま使う）。frame_codec.py の形
--     result : Actions が書く、予想タブに出す結果（final・買い目・比較用・Claude の予想）。frame_codec.py の形
--   prediction_runner   : 予想ボタンをどこで計算するか（1行だけ）。actions（既定）/ app（アプリの中）。管理者が設定タブで切り替える
--
-- 既存の表・ビュー（prediction_snapshots / prediction_results / manshu_* / odds_* など）は変えない。
-- 予想の保存は今までどおり save_latest_prediction_snapshot()（Actions が呼ぶ。締切後は上書きしない）。
--
-- 新しい表は、ほかの新しい表（auto_post_log・manshu_memos など）と同じく RLS を有効にして直接は読み書きさせず、
-- アプリは下の関数だけを使う。Actions は service_role で書く。
-- 起動に使うトークンは pg_cron と同じ vault の github_track_odds_token（boatrace-actions-dispatch）。
-- GitHub Actions の実行記録は誰でも見られるので、起動の入力は依頼の id だけ（会場・レース・名前は送らない）。

create table if not exists public.prediction_requests (
  id uuid primary key default gen_random_uuid(),
  race_key text not null,
  mode text not null check (mode in ('model', 'claude', 'avg')),
  status text not null default 'queued' check (status in ('queued', 'running', 'done', 'error')),
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  role text not null default 'staff' check (role in ('admin', 'staff')),
  collector_name text,
  input jsonb,
  result jsonb,
  save_status text,           -- save_latest_prediction_snapshot などの結果（inserted / updated / closed など）
  timing text                 -- 段階ごとの時間（[TIMING] と同じ。確認用）
);

create index if not exists prediction_requests_created_at_idx on public.prediction_requests (created_at);
create index if not exists prediction_requests_race_key_idx on public.prediction_requests (race_key);

alter table public.prediction_requests enable row level security;
revoke all on public.prediction_requests from anon, authenticated;

create table if not exists public.prediction_runner (
  id smallint primary key default 1 check (id = 1),
  runner text not null default 'actions' check (runner in ('actions', 'app')),
  updated_at timestamptz not null default now()
);

-- 最初は「アプリの中」で始め、Actions での計算を1回確かめてから actions に切り替える
-- （新しいワークフローは main に入るまで起動できないため）
insert into public.prediction_runner (id, runner) values (1, 'app') on conflict (id) do nothing;

alter table public.prediction_runner enable row level security;
revoke all on public.prediction_runner from anon, authenticated;

create or replace function public.prediction_runner_get()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select coalesce((select runner from public.prediction_runner where id = 1), 'actions');
$$;

create or replace function public.prediction_runner_set(p_runner text)
returns text
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_runner not in ('actions', 'app') then
    raise exception 'runner must be actions or app';
  end if;
  insert into public.prediction_runner (id, runner, updated_at) values (1, p_runner, now())
  on conflict (id) do update set runner = excluded.runner, updated_at = excluded.updated_at;
  return p_runner;
end;
$$;

-- 依頼を記録して Actions を起動する。戻り値は依頼の id。
-- 同じレース・同じボタン・同じ役割の依頼が3分以内に queued / running なら、新しく起動せずその id を返す。
-- 10分間に60件を超える依頼は断る（起動しすぎの歯止め）。7日より古い依頼は消す。
create or replace function public.prediction_request_create(
  p_race_key text, p_mode text, p_role text, p_collector_name text, p_input jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
begin
  if p_race_key !~ '^[0-9]{8}_[0-9]{2}_[0-9]{1,2}$' then
    raise exception 'bad race_key';
  end if;
  if p_mode not in ('model', 'claude', 'avg') or p_role not in ('admin', 'staff') then
    raise exception 'bad mode or role';
  end if;
  if octet_length(p_input::text) > 2000000 then
    raise exception 'input too large';
  end if;

  delete from public.prediction_requests where created_at < now() - interval '7 days';

  select id into v_id from public.prediction_requests
  where race_key = p_race_key and mode = p_mode and role = p_role
    and status in ('queued', 'running') and created_at > now() - interval '3 minutes'
  order by created_at desc limit 1;
  if v_id is not null then
    return v_id;
  end if;

  if (select count(*) from public.prediction_requests where created_at > now() - interval '10 minutes') >= 60 then
    raise exception 'too many prediction requests';
  end if;

  insert into public.prediction_requests (race_key, mode, role, collector_name, input)
  values (p_race_key, p_mode, p_role, left(coalesce(p_collector_name, ''), 100), p_input)
  returning id into v_id;

  perform net.http_post(
    url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/predict_race.yml/dispatches',
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
    body := jsonb_build_object('ref', 'main', 'inputs', jsonb_build_object('request_id', v_id::text)),
    timeout_milliseconds := 10000
  );
  return v_id;
end;
$$;

-- 依頼の状態（アプリが5秒おきに確かめる）。result は done のときだけ返す。
create or replace function public.prediction_request_status(p_id uuid)
returns table (status text, error text, created_at timestamptz, started_at timestamptz, finished_at timestamptz,
               save_status text, result jsonb)
language sql
stable
security definer
set search_path = public
as $$
  select r.status, r.error, r.created_at, r.started_at, r.finished_at, r.save_status,
         case when r.status = 'done' then r.result end
  from public.prediction_requests r
  where r.id = p_id;
$$;

revoke all on function public.prediction_runner_get() from public;
revoke all on function public.prediction_runner_set(text) from public;
revoke all on function public.prediction_request_create(text, text, text, text, jsonb) from public;
revoke all on function public.prediction_request_status(uuid) from public;
grant execute on function public.prediction_runner_get() to anon, authenticated, service_role;
grant execute on function public.prediction_runner_set(text) to anon, authenticated, service_role;
grant execute on function public.prediction_request_create(text, text, text, text, jsonb) to anon, authenticated, service_role;
grant execute on function public.prediction_request_status(uuid) to anon, authenticated, service_role;
