-- 記事表示ページ（?view=note、note_page.py）の改良と、1日の締めの結果報告記事。既存の関数は消さない（追加だけ）。
--
--  1. note_view_articles_v2: note_view_articles に、見出し画像（header_image・理由）・公開した記事の URL・公開時刻・
--     万舟チャンス／展示前厳選のレースか、を足したもの。記事表示ページはこちらを使う（古い note_view_articles は残す）
--  2. note_view_register: 記事表示ページから、公開した note 記事の URL を登録する（キーが合うときだけ）。
--     自動下書き（AUTO_DRAFT）は status = PUBLISHED・本日N本目の番号を振り、確定済みの記事・夜のまとめ（的中判定）に入れる。
--     手動で確定した記事（DRAFT）も PUBLISHED にして URL を残す。URL は https://note.com/<名前>/n/<記事ID> の形だけ受け付ける
--     戻り値: registered（初めて登録）/ updated（URL を変えた）/ invalid_url / not_found（キー違いを含む）
--  3. note_report_notify: 1日の締めに、結果報告記事のページ（?view=note&report=日付&key=…）の URL を Discord に送る。
--     pg_cron で毎日 JST 22:40（UTC 13:40）。自動下書きが ON・URL 設定済み・その日に確定済み（公開済み）の記事があるときだけ
--  見出し画像は自動下書きを作るとき（note_auto.py）に、手動の記事と同じ規則（note_header.py）で選んで set_note_header_image で記録する。

create or replace function public.note_view_articles_v2(p_key text, p_race_key text default null, p_date date default null)
returns table (
  race_key text, publication_date date, venue text, race_no integer, title text, article_text text,
  price integer, status text, ticket_source text, article_writer text, created_at timestamptz, deadline text,
  sequence_no integer, header_image text, header_image_reason text, note_url text, published_at timestamptz,
  is_manshu boolean, is_pre boolean
)
language sql
stable
security definer
set search_path to 'public'
as $function$
  select n.race_key, n.publication_date, n.venue, n.race_no, n.title, n.article_text,
         n.price, n.status, n.ticket_source, n.article_writer, n.created_at,
         coalesce((select p.deadline from public.pre_exhibition_picks p where p.race_key = n.race_key limit 1),
                  (select m.deadline from public.manshu_chance_picks m where m.race_key = n.race_key limit 1)) as deadline,
         n.sequence_no, n.header_image, n.header_image_reason, n.note_url, n.published_at,
         exists (select 1 from public.manshu_chance_picks m where m.race_key = n.race_key) as is_manshu,
         exists (select 1 from public.pre_exhibition_picks p where p.race_key = n.race_key) as is_pre
  from public.note_publications n
  where public.note_view_key_ok(p_key)
    and (case when nullif(p_race_key, '') is not null then n.race_key = p_race_key
              else n.publication_date = coalesce(p_date, (now() at time zone 'Asia/Tokyo')::date)
                   and (n.status = 'AUTO_DRAFT' or n.trigger_reason = '自動下書き（締切前の通知）') end)
  order by deadline nulls last, n.race_key;
$function$;

revoke all on function public.note_view_articles_v2(text, text, date) from public;
grant execute on function public.note_view_articles_v2(text, text, date) to anon, authenticated;

create or replace function public.note_view_register(p_key text, p_race_key text, p_note_url text)
returns text
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  ex public.note_publications;
  v_url text := trim(coalesce(p_note_url, ''));
  next_no integer;
begin
  if not public.note_view_key_ok(p_key) then
    return 'not_found';
  end if;
  if v_url !~ '^https://note\.com/[A-Za-z0-9_-]+/n/[A-Za-z0-9]+([/?#][^\s]*)?$' or length(v_url) > 300 then
    return 'invalid_url';
  end if;
  select * into ex from public.note_publications where race_key = p_race_key for update;
  if not found then
    return 'not_found';
  end if;
  if ex.status = 'AUTO_DRAFT' then
    perform pg_advisory_xact_lock(hashtext(ex.publication_date::text));
    select coalesce(max(sequence_no), 0) + 1 into next_no
    from public.note_publications where publication_date = ex.publication_date and sequence_no > 0;
    update public.note_publications set
      status = 'PUBLISHED', sequence_no = next_no, note_url = v_url, published_at = now()
    where id = ex.id;
    return 'registered';
  end if;
  update public.note_publications set
    status = 'PUBLISHED', note_url = v_url, published_at = coalesce(published_at, now())
  where id = ex.id;
  return case when ex.note_url is null then 'registered' else 'updated' end;
end;
$function$;

revoke all on function public.note_view_register(text, text, text) from public;
grant execute on function public.note_view_register(text, text, text) to anon, authenticated;

create or replace function public.note_report_notify()
returns integer
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  v_url text;
  v_on boolean;
  v_base text;
  v_key text;
  v_today date := (now() at time zone 'Asia/Tokyo')::date;
  v_n int;
begin
  select coalesce(note_auto_enabled, false), nullif(trim(note_view_base_url), '') into v_on, v_base
  from public.app_settings where id = 1;
  select decrypted_secret into v_url from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  select decrypted_secret into v_key from vault.decrypted_secrets where name = 'note_view_key' limit 1;
  if not coalesce(v_on, false) or v_base is null or v_key is null or v_url is null then
    return 0;
  end if;
  select count(*) into v_n from public.note_publications
  where publication_date = v_today and status <> 'AUTO_DRAFT';
  if v_n = 0 then
    return 0;
  end if;
  perform net.http_post(
    url := v_url,
    body := jsonb_build_object('content', format(
      E'📊 **本日の結果報告記事**（note記事 %s本）\n<%s/?view=note&report=%s&key=%s>',
      v_n, rtrim(v_base, '/'), v_today, v_key))
  );
  return 1;
end;
$function$;

revoke all on function public.note_report_notify() from public, anon, authenticated;

select cron.schedule('note-report-notify', '40 13 * * *', 'select public.note_report_notify();');
