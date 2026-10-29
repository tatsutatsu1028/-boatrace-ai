-- スレッズのインサイト（閲覧数・いいね等）を毎日1回ためる（threads_insights.py。track_odds.yml の1日1回のステップ）。
--
--   threads_posts            : 自分の投稿1件1行。投稿日時・投稿の種類・本文の先頭・どのレースの投稿か
--     kind  morning        : 朝の自動投稿（threads_auto_posts。post_index は何投稿目か）
--           result         : 朝の3レースの結果の自動返信（threads_result_posts）
--           result_summary : 朝の3レースの結果まとめの自動返信（threads_result_posts の summary）
--           race           : アプリから1レースずつ投稿（本文の型から。style は normal / aim）
--           summary        : アプリの「📊 本日のまとめ投稿」
--           manual         : スレッズのアプリから直接（上のどれにも当たらない投稿）
--           manual_reply   : スレッズのアプリから直接の返信（コメントへの返事など）
--     source  どこから種類が分かったか（auto_posts / result_posts / app / text / none）
--             朝の自動投稿と結果の返信は threads_auto_posts / threads_result_posts の投稿IDで紐付ける
--   threads_post_insights    : 投稿ごとの数値の推移。投稿から14日以内の投稿を毎日1回（1投稿1日1行）
--   threads_account_insights : アカウント全体の数値（1日1行）。views は前日（JST）の閲覧数、
--                              followers_count は取った時点のフォロワー数。この行が「その日の取得が終わった」印
--
-- 書くのは GitHub Actions（service_role）と管理者のアプリ（Secret key）だけ。
-- RLS を有効にして anon・authenticated には何も許さない（threads_race_posts と同じ）。

create table if not exists public.threads_posts (
  post_id text primary key,
  posted_at timestamptz,
  posted_date date,                      -- 投稿日（JST）
  kind text not null default 'manual'
    check (kind in ('morning', 'result', 'result_summary', 'race', 'summary', 'manual', 'manual_reply')),
  source text,
  post_index smallint,
  race_key text,
  race_date date,
  venue text,
  race_no smallint,
  style text,
  text_head text,                        -- 本文の先頭（80文字）
  text_length integer,
  media_type text,
  is_reply boolean,
  permalink text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists threads_posts_posted_at_idx on public.threads_posts (posted_at);
create index if not exists threads_posts_race_key_idx on public.threads_posts (race_key);

create table if not exists public.threads_post_insights (
  post_id text not null,
  snapshot_date date not null,           -- 取った日（JST）
  fetched_at timestamptz not null default now(),
  age_hours numeric(7, 1),               -- 投稿から取った時点までの時間
  views integer,
  likes integer,
  replies integer,
  reposts integer,
  quotes integer,
  shares integer,
  primary key (post_id, snapshot_date)
);

create index if not exists threads_post_insights_snapshot_idx on public.threads_post_insights (snapshot_date);

create table if not exists public.threads_account_insights (
  snapshot_date date primary key,        -- 取った日（JST）
  fetched_at timestamptz,
  followers_count integer,
  views integer,                         -- views_date（前日・JST）の閲覧数
  views_date date,
  posts_collected integer,               -- この日に数値を取った投稿の数
  status text not null default 'running' check (status in ('running', 'done', 'failed')),
  attempts integer not null default 0,
  attempted_at timestamptz,              -- 取り始めた時刻（途中で止まった回を見分ける）
  error text
);

alter table public.threads_posts enable row level security;
alter table public.threads_post_insights enable row level security;
alter table public.threads_account_insights enable row level security;

-- 分析用: 数値の推移に投稿の情報を付けたもの
create or replace view public.threads_post_insights_view
with (security_invoker = true) as
select
  i.snapshot_date,
  i.age_hours,
  p.post_id,
  p.posted_at,
  p.posted_date,
  (p.posted_at at time zone 'Asia/Tokyo')::time as posted_time_jst,
  p.kind,
  p.post_index,
  p.race_key,
  p.venue,
  p.race_no,
  p.style,
  p.text_head,
  p.media_type,
  p.is_reply,
  i.views,
  i.likes,
  i.replies,
  i.reposts,
  i.quotes,
  i.shares
from public.threads_post_insights i
join public.threads_posts p using (post_id);
