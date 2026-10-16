-- スレッズの朝の自動投稿（threads_morning.py）と、note の記事（全て有料・載せる買い目の記録）。
--
--   app_settings.threads_auto_post_enabled : 朝の自動投稿の ON/OFF（初期値 OFF）
--   threads_auto_posts                     : 1日1行。投稿の成否・投稿したレース・買い目・失敗の理由
--   note_publications.ticket_source        : 記事に載せた買い目（model = モデルのみ予想 / avg = 両方の予想平均）
--   note_publications.article_tickets      : 記事に載せた買い目そのもの（夜のまとめの的中判定に使う）
--   note_publications.article_writer       : 本文を書いたもの（claude / template）
--
-- note は全て有料（300円）として記録する（1日3本までの無料枠はやめる）。今までの行はそのまま。
-- threads_auto_posts を書くのは GitHub Actions（service_role）だけ。アプリには読み取りだけ許す。

alter table public.app_settings
  add column if not exists threads_auto_post_enabled boolean not null default false;

create table if not exists public.threads_auto_posts (
  race_date date primary key,
  -- posted = 3投稿とも成功 / partial = 途中で失敗 / failed = 1投稿目から失敗 / skipped = 投稿しなかった
  status text not null check (status in ('posted', 'partial', 'failed', 'skipped')),
  reason text,                         -- 投稿しなかった理由・失敗の理由
  ticket_source text not null default 'model',
  posts jsonb,                         -- [{pick_rank, race_key, venue, race_no, deadline, favorite_lane,
                                       --   hit_probability, tickets:[{combo, group}], text, post_id, error}]
  race_keys text[],
  note_overlap text[],                 -- 投稿時点で note の記事にもなっていたレース
  attempts integer not null default 1,
  attempted_at timestamptz not null default now(),
  posted_at timestamptz
);

alter table public.threads_auto_posts enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'threads_auto_posts'
                 and policyname = 'threads_auto_posts_read') then
    create policy threads_auto_posts_read on public.threads_auto_posts
      for select to anon, authenticated using (true);
  end if;
end $$;

alter table public.note_publications
  add column if not exists ticket_source text not null default 'model',
  add column if not exists article_tickets jsonb,
  add column if not exists article_writer text;

-- 記事枠の確定（アプリはこれを呼ぶ）。同じレースは何度押しても1行。全て有料（300円）。
create or replace function public.reserve_note_publication_v2(
  p_race_key text, p_publication_date date, p_venue text, p_race_no integer, p_trigger_reason text,
  p_p1_lane integer, p_p1_prob double precision, p_title text, p_article_text text,
  p_ticket_source text, p_article_tickets jsonb, p_article_writer text
)
returns public.note_publications
language plpgsql
set search_path to ''
as $function$
declare
  existing public.note_publications;
  next_no integer;
  created public.note_publications;
begin
  select * into existing from public.note_publications where race_key = p_race_key;
  if found then
    return existing;
  end if;
  perform pg_advisory_xact_lock(hashtext(p_publication_date::text));
  select coalesce(max(sequence_no), 0) + 1 into next_no
  from public.note_publications where publication_date = p_publication_date;
  insert into public.note_publications (
    race_key, publication_date, venue, race_no, sequence_no,
    publication_type, price, trigger_reason, p1_lane, p1_prob,
    title, article_text, ticket_source, article_tickets, article_writer
  ) values (
    p_race_key, p_publication_date, p_venue, p_race_no, next_no,
    'PAID', 300, p_trigger_reason, p_p1_lane, p_p1_prob, p_title, p_article_text,
    case when p_ticket_source = 'avg' then 'avg' else 'model' end,
    p_article_tickets, p_article_writer
  )
  returning * into created;
  return created;
end;
$function$;

-- 今までの関数（デプロイ前のアプリが呼ぶ）も有料で記録する
create or replace function public.reserve_note_publication(
  p_race_key text, p_publication_date date, p_venue text, p_race_no integer, p_trigger_reason text,
  p_p1_lane integer, p_p1_prob double precision, p_title text, p_article_text text
)
returns public.note_publications
language sql
set search_path to ''
as $function$
  select * from public.reserve_note_publication_v2(
    p_race_key, p_publication_date, p_venue, p_race_no, p_trigger_reason,
    p_p1_lane, p_p1_prob, p_title, p_article_text, 'model', null, null);
$function$;

grant execute on function public.reserve_note_publication_v2(
  text, date, text, integer, text, integer, double precision, text, text, text, jsonb, text
) to anon, authenticated;

-- 確定した記事の買い目・本文を差し替える（アプリは締切前だけ呼ぶ）。
create or replace function public.update_note_publication_article(
  p_race_key text, p_ticket_source text, p_article_tickets jsonb,
  p_title text, p_article_text text, p_article_writer text
)
returns public.note_publications
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  updated public.note_publications;
begin
  update public.note_publications set
    ticket_source = case when p_ticket_source = 'avg' then 'avg' else 'model' end,
    article_tickets = p_article_tickets,
    title = coalesce(p_title, title),
    article_text = coalesce(p_article_text, article_text),
    article_writer = p_article_writer
  where race_key = p_race_key
  returning * into updated;
  return updated;
end;
$function$;

grant execute on function public.update_note_publication_article(text, text, jsonb, text, text, text)
  to anon, authenticated;

-- pg_cron による threads_morning.yml（朝の無料3レースの自動投稿）の起動: 毎日 JST 09:00（UTC 00:00）。
-- 厳選（trigger-pre-picks 08:05）が終わっていなければ、threads_morning.py が終わるのを待つ（最大40分）。
-- ON/OFF はアプリの設定（app_settings.threads_auto_post_enabled）。止めるときは
-- select cron.unschedule('trigger-threads-morning');
select cron.schedule(
  'trigger-threads-morning',
  '0 0 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/threads_morning.yml/dispatches',
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
