-- オリジナル展示の記録（original_exhibition_log）に、締切と「値を締切前に取れたか」を足す。何も削除しない。
--
--   deadline        : 締切（'HH:MM'。展示後の自動予想が使う当日の締切）
--   before_deadline : 値を締切前に取れたか。取れなかったレース（未公開・形式違い・取得失敗・対象外）と
--                     締切が読めないレースは null
--
-- これまでの記録も埋める: 締切は auto_post_log（無ければ manshu_chance_picks）、取った時刻は original_exhibition.fetched_at。

alter table public.original_exhibition_log
  add column if not exists deadline text,
  add column if not exists before_deadline boolean;

with src as (
  select l.race_key,
         coalesce(a.deadline, m.deadline) as deadline,
         (select max(e.fetched_at) from public.original_exhibition e where e.race_key = l.race_key) as fetched_at
  from public.original_exhibition_log l
  left join public.auto_post_log a on a.race_key = l.race_key
  left join public.manshu_chance_picks m on m.race_key = l.race_key
  where l.deadline is null
)
update public.original_exhibition_log l
set deadline = src.deadline,
    before_deadline = case
      when l.status <> 'ok' or src.fetched_at is null or src.deadline !~ '^\d{1,2}:\d{2}$' then null
      else src.fetched_at < ((l.race_date + src.deadline::time) at time zone 'Asia/Tokyo')
    end
from src
where src.race_key = l.race_key;
