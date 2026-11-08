-- 結果報告記事（?view=note&report=日付、note_page.py）を note に公開したら、その結果を Threads にも自動投稿する
-- （note_report_threads.py）。何も削除しない。
--
--   app_settings.note_report_threads_enabled : 自動投稿の ON/OFF（既定 OFF。設定タブで切り替え）
--   note_report_posts                        : 1日1行。結果報告記事の note の URL と、Threads への投稿の状態
--       status: posting（投稿中）/ posted（投稿済み）/ failed（失敗）。null は URL だけ登録（OFF のとき）
--   note_report_register(key, 日付, URL)     : URL を登録し、投稿してよいかを返す（キーが合うときだけ）
--       投稿済みの日は二度と投稿しない（URL を登録し直しても posted のまま）。失敗した日は登録し直すとやり直す。
--       投稿中のまま10分たった日（アプリが途中で止まったなど）もやり直せる
--   note_report_post_done(key, 日付, 成否, …)  : 投稿の結果を記録し、Discord（厳選10のチャンネル）に成功・失敗を送る
--   note_report_status(key, 日付)            : 結果報告ページに出す状態
-- 表は RLS で anon から直接読み書きさせない（上の関数を通す）。

alter table public.app_settings
  add column if not exists note_report_threads_enabled boolean not null default false;

create table if not exists public.note_report_posts (
  race_date date primary key,
  note_url text,
  status text check (status in ('posting', 'posted', 'failed')),
  post_id text,
  post_text text,
  error text,
  attempts integer not null default 0,
  registered_at timestamptz,
  posted_at timestamptz,
  updated_at timestamptz not null default now()
);

alter table public.note_report_posts enable row level security;

create or replace function public.note_report_register(p_key text, p_date date, p_note_url text)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  ex public.note_report_posts;
  v_url text := trim(coalesce(p_note_url, ''));
  v_on boolean;
  v_reg text;
begin
  if not public.note_view_key_ok(p_key) or p_date is null then
    return jsonb_build_object('result', 'not_found');
  end if;
  if v_url !~ '^https://note\.com/[A-Za-z0-9_-]+/n/[A-Za-z0-9]+([/?#][^\s]*)?$' or length(v_url) > 300 then
    return jsonb_build_object('result', 'invalid_url');
  end if;
  insert into public.note_report_posts (race_date) values (p_date) on conflict do nothing;
  select * into ex from public.note_report_posts where race_date = p_date for update;
  v_reg := case when ex.note_url is null then 'registered' else 'updated' end;
  update public.note_report_posts set note_url = v_url, registered_at = coalesce(registered_at, now()),
    updated_at = now() where race_date = p_date;
  select coalesce(note_report_threads_enabled, false) into v_on from public.app_settings where id = 1;
  if ex.status = 'posted' then
    return jsonb_build_object('result', v_reg, 'post', 'posted');
  end if;
  if ex.status = 'posting' and ex.updated_at > now() - interval '10 minutes' then
    return jsonb_build_object('result', v_reg, 'post', 'posting');
  end if;
  if not coalesce(v_on, false) then
    return jsonb_build_object('result', v_reg, 'post', 'off');
  end if;
  update public.note_report_posts set status = 'posting', attempts = attempts + 1, error = null, updated_at = now()
  where race_date = p_date;
  return jsonb_build_object('result', v_reg, 'post', 'claimed');
end;
$function$;

create or replace function public.note_report_post_done(
  p_key text, p_date date, p_ok boolean, p_post_id text, p_text text, p_error text
)
returns text
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  ex public.note_report_posts;
  v_hook text;
  v_msg text;
begin
  if not public.note_view_key_ok(p_key) then
    return 'not_found';
  end if;
  select * into ex from public.note_report_posts where race_date = p_date for update;
  -- 投稿中（note_report_register が渡した）の日だけ記録する。Discord を何度も鳴らさないため
  if not found or ex.status is distinct from 'posting' then
    return 'ignored';
  end if;
  update public.note_report_posts set
    status = case when p_ok then 'posted' else 'failed' end,
    post_id = case when p_ok then left(p_post_id, 100) else post_id end,
    post_text = left(p_text, 1000), error = case when p_ok then null else left(p_error, 300) end,
    posted_at = case when p_ok then now() else posted_at end, updated_at = now()
  where race_date = p_date;
  select decrypted_secret into v_hook from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  if v_hook is not null then
    v_msg := case when p_ok
      then format(E'🧵 %s の結果報告を Threads に投稿しました\n📝 <%s>', to_char(p_date, 'FMMM/FMDD'), ex.note_url)
      else format(E'⚠️ %s の結果報告の Threads 投稿に失敗しました: %s\n結果報告ページで URL を登録し直すと、もう一度投稿します',
                  to_char(p_date, 'FMMM/FMDD'), left(coalesce(p_error, '不明'), 200)) end;
    perform net.http_post(url := v_hook, body := jsonb_build_object('content', left(v_msg, 1900)));
  end if;
  return case when p_ok then 'posted' else 'failed' end;
end;
$function$;

create or replace function public.note_report_status(p_key text, p_date date)
returns table (race_date date, note_url text, status text, post_id text, error text, attempts integer,
               posted_at timestamptz, enabled boolean)
language sql
stable
security definer
set search_path to 'public'
as $function$
  select p.race_date, p.note_url, p.status, p.post_id, p.error, p.attempts, p.posted_at,
         (select coalesce(note_report_threads_enabled, false) from public.app_settings where id = 1)
  from public.note_report_posts p
  where public.note_view_key_ok(p_key) and p.race_date = p_date;
$function$;

revoke all on function public.note_report_register(text, date, text) from public;
revoke all on function public.note_report_post_done(text, date, boolean, text, text, text) from public;
revoke all on function public.note_report_status(text, date) from public;
grant execute on function public.note_report_register(text, date, text) to anon, authenticated;
grant execute on function public.note_report_post_done(text, date, boolean, text, text, text) to anon, authenticated;
grant execute on function public.note_report_status(text, date) to anon, authenticated;
