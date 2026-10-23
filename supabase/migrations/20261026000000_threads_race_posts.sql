-- アプリからのスレッズ投稿（1レースずつ）の記録。二重投稿を防ぐのに使う（threads_view.py）。
--
--   予想タブと「💥 万舟チャンス」「🌅 展示前厳選10」の一覧の「🧵 スレッズ投稿」で投稿したら1行書く。
--   同じレースで投稿済みなら、アプリは「投稿済み（時刻）」と出し、もう一度投稿するときは確認を挟む。
--   朝9:00の自動投稿（threads_auto_posts）とは別の表（自動投稿の記録はそのまま）。
--
--   screen        : 投稿した画面（predict = 予想タブ / list = 一覧）
--   style         : スレッズの文章（normal = 通常の予想 / aim = 中穴〜万舟狙い）
--   ticket_source : 投稿したときの note記事の買い目（model / avg / aim。記事にしていなければ null）
--   prediction    : 使った予想（例: 展示後の自動予想 / 朝の予想（展示前））
--
-- スレッズの設定（threads_config）と同じく、アプリは Secret key（service_key）で読み書きする。
-- RLS を有効にして anon・authenticated には何も許さない（service_role は RLS を通らない）。

create table if not exists public.threads_race_posts (
  id bigint generated always as identity primary key,
  race_key text not null,
  race_date date not null,
  post_id text,
  posted_at timestamptz not null default now(),
  screen text check (screen in ('predict', 'list')),
  style text check (style in ('normal', 'aim')),
  ticket_source text,
  prediction text,
  text text
);

create index if not exists threads_race_posts_race_date_idx on public.threads_race_posts (race_date);
create index if not exists threads_race_posts_race_key_idx on public.threads_race_posts (race_key);

alter table public.threads_race_posts enable row level security;
