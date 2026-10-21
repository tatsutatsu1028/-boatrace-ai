-- note記事に「中穴〜万舟狙いの買い目」（ticket_source = 'aim'）を載せられるようにする。
-- 万舟チャンスのレースで、予想タブの note記事の欄から選ぶ（note_view.py）。記事に載せた買い目は今までどおり
-- article_tickets に入り（区分は group = 中穴 / 万舟）、夜のまとめの的中判定もそれで行う。
-- 2つの関数の ticket_source の受け付けに 'aim' を足すだけ（他は 20261017000000 と同じ）。

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
    case when p_ticket_source in ('avg', 'aim') then p_ticket_source else 'model' end,
    p_article_tickets, p_article_writer
  )
  returning * into created;
  return created;
end;
$function$;

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
    ticket_source = case when p_ticket_source in ('avg', 'aim') then p_ticket_source else 'model' end,
    article_tickets = p_article_tickets,
    title = coalesce(p_title, title),
    article_text = coalesce(p_article_text, article_text),
    article_writer = p_article_writer
  where race_key = p_race_key
  returning * into updated;
  return updated;
end;
$function$;

