-- スレッズの朝の投稿の1投稿目に付ける「本日の厳選3レース」画像（threads_morning.py）。
--
--   storage バケット threads-images（公開）: morning/<日付>_<時刻>.png
--     スレッズは画像を公開URLから取りに行くため公開にする（載せるのは投稿する画像だけ）。
--     書き込みは GitHub Actions（service_role）だけ。一覧の取得は許さない（storage.objects にポリシーを作らない）
--   threads_auto_posts.image_url   : 1投稿目に付けた画像の公開URL（付けられなかった日は null）
--   threads_auto_posts.image_error : 画像を付けられなかった理由（画像なしで投稿した）

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('threads-images', 'threads-images', true, 5242880, array['image/png'])
on conflict (id) do update set public = true, file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

alter table public.threads_auto_posts
  add column if not exists image_url text,
  add column if not exists image_error text;
