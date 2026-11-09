-- 万舟チャンスのレースのオリジナル展示（一周・まわり足・直線）の自動取得の記録（original_exhibition_fetcher.py）。
-- 記録のみ。予想・頭本命・買い目・通知には使わない。既存の手入力（予想タブ）とは別の表で、何も変えない。
--
--   original_exhibition     : 1艇1行。各場の公式サイトの表から取った値（計測なし「-」は null）
--   original_exhibition_log : 1レース1行。取れたか・取れなかった理由
--       status = ok（取れた）/ unpublished（未公開）/ format（形式違い）/ error（取得失敗）
--                / unsupported（対象外の場。通信しない）
--       attempts = 取りに行った回数（1回 + 未公開・取得失敗のときだけ取り直し1回）
--
-- 書くのは展示後の自動予想（auto_post_predict.yml。service_role）だけ。
-- RLS を有効にして anon・authenticated には何も許さない（service_role は RLS を通らない）。

create table if not exists public.original_exhibition (
  race_key text not null,
  lane smallint not null check (lane between 1 and 6),
  original_lap double precision,        -- 一周（秒）
  original_turn double precision,       -- まわり足（秒）
  original_straight double precision,   -- 直線（秒）
  source_url text,
  fetched_at timestamptz not null default now(),
  primary key (race_key, lane)
);

create table if not exists public.original_exhibition_log (
  race_key text primary key,
  race_date date not null,
  jcd text not null,
  race_no smallint not null,
  status text not null check (status in ('ok', 'unpublished', 'format', 'error', 'unsupported')),
  reason text,
  source_url text,
  attempts integer not null default 0,
  last_tried_at timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists original_exhibition_log_race_date_idx on public.original_exhibition_log (race_date);

alter table public.original_exhibition enable row level security;
alter table public.original_exhibition_log enable row level security;
revoke all on public.original_exhibition from anon, authenticated;
revoke all on public.original_exhibition_log from anon, authenticated;
