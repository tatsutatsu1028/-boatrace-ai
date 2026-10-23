-- スレッズの朝の自動投稿で出した3レースの結果の自動投稿（threads_results.py。track_odds.yml の結果確定のあと）。
--
--   app_settings.threads_result_post_enabled : 結果の自動投稿の ON/OFF（初期値 OFF）。
--                                              朝の自動投稿（threads_auto_post_enabled）も ON の日だけ投稿する
--   threads_result_posts                     : 1日に最大4行（レースごとの結果の投稿3行 + まとめ1行）
--     item    : レースは race_key、まとめは 'summary'。同じ (race_date, item) は1回だけ投稿する
--     status  waiting : 結果待ち（公式の結果ページを確かめた時刻 checked_at だけ残す）
--             posting : 投稿中（この行を取ってから投稿する。途中で止まったら二重投稿を防ぐため出し直さない）
--             posted  : 投稿した
--             failed  : 失敗（attempts が3回になるまで次の回にやり直す。3回で止めて reason に理由）
--     outcome hit / miss / cancelled（中止） / refunded（返還）
--
-- 書くのは GitHub Actions（service_role）だけ。アプリには読み取りだけ許す（threads_auto_posts と同じ）。

alter table public.app_settings
  add column if not exists threads_result_post_enabled boolean not null default false;

create table if not exists public.threads_result_posts (
  race_date date not null,
  item text not null,
  post_index smallint,                  -- 朝の何投稿目のレースか（まとめは null）
  race_key text,
  venue text,
  race_no smallint,
  outcome text check (outcome in ('hit', 'miss', 'cancelled', 'refunded')),
  trifecta_actual text,                 -- 3連単の結果（同着は空白区切り）
  trifecta_payout integer,              -- 3連単の配当（100円あたり。同着は大きいほう）
  hit_combos text[],                    -- 当たった買い目
  hits integer,                         -- まとめ: 的中レース数
  settled integer,                      -- まとめ: 結果の出たレース数（中止・返還を除く）
  voids integer,                        -- まとめ: 中止・返還のレース数
  status text not null check (status in ('waiting', 'posting', 'posted', 'failed')),
  reason text,
  error text,
  text text,
  reply_to text,                        -- 返信先の投稿ID（そのレースの朝の投稿・まとめは1投稿目）
  post_id text,
  attempts integer not null default 0,
  checked_at timestamptz,               -- 公式の結果ページを最後に確かめた時刻
  attempted_at timestamptz,
  posted_at timestamptz,
  created_at timestamptz not null default now(),
  primary key (race_date, item)
);

alter table public.threads_result_posts enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'threads_result_posts'
                 and policyname = 'threads_result_posts_read') then
    create policy threads_result_posts_read on public.threads_result_posts
      for select to anon, authenticated using (true);
  end if;
end $$;
