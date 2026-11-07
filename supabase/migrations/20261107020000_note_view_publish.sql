-- 記事表示ページ（?view=note、note_page.py）の改良。
--
--  1. note_view_articles が見出し画像（header_image・理由）・公開した記事の URL・公開時刻・万舟チャンス／展示前厳選のレースか
--     も返す（戻り値の列が変わるので作り直す）
--  2. note_view_register: 記事表示ページから、公開した note 記事の URL を登録する（キーが合うときだけ）。
--     自動下書き（AUTO_DRAFT）は status = PUBLISHED・本日N本目の番号を振り、確定済みの記事・夜のまとめ（的中判定）に入れる。
--     手動で確定した記事（DRAFT）も PUBLISHED にして URL を残す。URL は https://note.com/<名前>/n/<記事ID> の形だけ受け付ける
--     戻り値: registered（初めて登録）/ updated（URL を変えた）/ invalid_url / not_found（キー違いを含む）
--  見出し画像は自動下書きを作るとき（note_auto.py）に、手動の記事と同じ規則（note_header.py）で選んで set_note_header_image で記録する。

drop function if exists public.note_view_articles(text, text, date);

create function public.note_view_articles(p_key text, p_race_key text default null, p_date date default null)
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
  where length(coalesce(p_key, '')) >= 16
    and exists (select 1 from vault.decrypted_secrets s
                where s.name = 'note_view_key' and s.decrypted_secret = p_key)
    and (case when nullif(p_race_key, '') is not null then n.race_key = p_race_key
              else n.publication_date = coalesce(p_date, (now() at time zone 'Asia/Tokyo')::date)
                   and (n.status = 'AUTO_DRAFT' or n.trigger_reason = '自動下書き（締切前の通知）') end)
  order by deadline nulls last, n.race_key;
$function$;

revoke all on function public.note_view_articles(text, text, date) from public;
grant execute on function public.note_view_articles(text, text, date) to anon, authenticated;

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
