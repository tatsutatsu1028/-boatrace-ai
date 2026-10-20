-- 「💥 万舟チャンス」の中穴〜万舟狙いの買い目を作れなかった理由と、朝の取り直し。
--
--   aim_error    : 買い目を作れなかった理由（例: オッズがまだそろっていません（120通り中80通り））。作れたら空
--   aim_tried_at : 最後に作ろうとした時刻（朝の選定・取り直し）
--
-- 10/20 は朝の選定（08:05）で10レース中8レースの買い目が作れず、理由も残らなかった。
-- 朝の時点でオッズが取れなかったレースは、08:30・09:00 JST に pre_picks.yml（task=aim）で
-- 買い目が空のレースだけ取り直す（公式サイトへは空のレースの数だけ。開始間隔3.4秒以上）。
--
-- 点検用のビュー manshu_aim_missing: 今日の万舟チャンスなのに買い目が空のレース（締切前・理由つき）。
-- 前提・止め方は 20261016000100_trigger_pre_picks_via_pg_cron.sql と同じ。

alter table public.manshu_chance_picks
  add column if not exists aim_error text,
  add column if not exists aim_tried_at timestamptz;

create or replace view public.manshu_aim_missing
with (security_invoker = true) as
select race_key, race_date, venue, race_no, deadline, pick_rank,
       coalesce(aim_error, '理由の記録なし') as aim_error, aim_tried_at
from public.manshu_chance_picks
where race_date = (now() at time zone 'Asia/Tokyo')::date
  and aim_tickets is null
  and result_at is null;

revoke all on public.manshu_aim_missing from anon, authenticated;

-- 画面で買い目を作ったら、作れなかった理由は消す（20261019000000_manshu_aim_tickets.sql の関数に1行足しただけ）
create or replace function public.manshu_save_aim(
  p_race_key text, p_tickets jsonb, p_method text, p_source text, p_odds_at timestamptz)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  pk record;
  v_deadline timestamptz;
begin
  select race_date, deadline into pk from public.manshu_chance_picks where race_key = p_race_key;
  if not found then
    return false;
  end if;
  if jsonb_typeof(p_tickets) <> 'array' or jsonb_array_length(p_tickets) > 20 then
    raise exception 'tickets must be an array of at most 20';
  end if;
  -- 締切後は書き換えない（結果を見てから差し替えられないように）
  begin
    v_deadline := (pk.race_date::text || ' ' || pk.deadline || ':00')::timestamp at time zone 'Asia/Tokyo';
  exception when others then
    v_deadline := null;
  end;
  if v_deadline is not null and now() >= v_deadline then
    return false;
  end if;
  update public.manshu_chance_picks set
    aim_tickets = p_tickets,
    aim_method = left(p_method, 20),
    aim_source = left(p_source, 20),
    aim_odds_at = p_odds_at,
    aim_saved_at = now(),
    aim_error = null
  where race_key = p_race_key and result_at is null;
  return found;
end;
$$;

revoke all on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) from public;
grant execute on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) to anon, authenticated, service_role;

-- 取り直し（cron の式は1つずつなので2つのジョブ）。止めるときは cron.unschedule でそれぞれ止める
select cron.schedule(
  'trigger-manshu-aim-retry-0830',
  '30 23 * * *',
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
  body := '{"ref":"main","inputs":{"task":"aim"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);

select cron.schedule(
  'trigger-manshu-aim-retry-0900',
  '0 0 * * *',
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
  body := '{"ref":"main","inputs":{"task":"aim"}}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);
