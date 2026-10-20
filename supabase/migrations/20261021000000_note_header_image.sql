-- note記事の見出し画像（note_header.py）。記事ごとに使った画像を残す。
--
--   note_publications.header_image        : 使った画像（manshu / kohaito / aresuimen / gensen / shobu）
--   note_publications.header_image_auto   : 自動で選んだ画像（自分で変えたかどうかが分かる）
--   note_publications.header_image_reason : 自動で選んだ理由（例: 万舟チャンスに選ばれたレース）
--   note_publications.header_image_at     : 記録した時刻
--
-- アプリ（管理者の note記事の欄）は表を直接書かず、下の関数 set_note_header_image を通して書く。

alter table public.note_publications
  add column if not exists header_image text,
  add column if not exists header_image_auto text,
  add column if not exists header_image_reason text,
  add column if not exists header_image_at timestamptz;

create or replace function public.set_note_header_image(
  p_race_key text, p_image text, p_auto text, p_reason text)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_image is null or p_image !~ '^[a-z0-9_]{1,20}$' or (p_auto is not null and p_auto !~ '^[a-z0-9_]{1,20}$') then
    raise exception 'invalid header image';
  end if;
  update public.note_publications set
    header_image = p_image,
    header_image_auto = p_auto,
    header_image_reason = left(p_reason, 200),
    header_image_at = now()
  where race_key = p_race_key;
  return found;
end;
$$;

revoke all on function public.set_note_header_image(text, text, text, text) from public;
grant execute on function public.set_note_header_image(text, text, text, text) to anon, authenticated, service_role;
