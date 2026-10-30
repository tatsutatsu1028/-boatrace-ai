-- 万舟チャンスのレースに、オーナーが自分で書いたメモ（自由記述）を付け、メモ入りの Claude 予想を比較に足す。
--
--   manshu_memos : 1レース1行（race_key・メモ・保存日時）。管理者が一覧のカードで書く（manshu_view.py）。
--                  書くのも読むのも下の関数だけ（表そのものは RLS で閉じる）。締切後は書き換えられない。
--                  空のメモで保存すると消す（そのレースは「メモなし」に戻る）
--   manshu_save_memo(race_key, memo)  : 保存（締切前・万舟チャンスのレースだけ）。戻り値 saved / deleted / closed / not_found。
--                  展示後の自動記録（manshu_source_compare の model の行）がもう済んでいれば、その場で
--                  auto_post_predict.yml（task=memo）を起動してメモ入りの Claude 予想を作り直す
--   manshu_memos_for_date(date)       : その日のメモと、メモ入りの Claude 予想を作った時刻・作れなかった理由
--
--   manshu_source_compare に4つ目の source 'claude_memo' を足す:
--     claude と同じ入力・同じ買い目ルール（中穴5＋万舟5）に、メモを「オーナーが自分で見た気配」として足した予想。
--     メモの無いレースは「記録なし」（tri_probs なし・買い目なし・memo_saved_at なし）で、集計に入れない
--     memo_saved_at : 予想に使ったメモの保存日時（saved_at が予想を作った日時）
--   manshu_source_summary に足す列:
--     *_memo4   : メモがあり、4種（model・claude・blend・claude_memo）がそろったレースだけの数字
--     no_memo   : claude_memo の「記録なし」（メモの無かった）レース数
--   all3 は今までどおり model・claude・blend の3種だけで数える。締切後に作ったもの（saved_at が締切以降）と、
--   締切後に保存されたメモで作ったもの（memo_saved_at が締切以降）は数えない
--
-- メモはスレッズ・note・Discord の文章には使わない（それらはこの表を読まない）。
-- 前提は 20261025000000_auto_post_predict.sql と同じ（vault の github_track_odds_token）。

create table if not exists public.manshu_memos (
  race_key text primary key,
  memo text not null check (char_length(memo) between 1 and 1000),
  saved_at timestamptz not null default now()
);

alter table public.manshu_memos enable row level security;
revoke all on public.manshu_memos from anon, authenticated;

-- 比較の4つ目
alter table public.manshu_source_compare drop constraint if exists manshu_source_compare_source_check;
alter table public.manshu_source_compare
  add constraint manshu_source_compare_source_check check (source in ('model', 'claude', 'blend', 'claude_memo'));
alter table public.manshu_source_compare add column if not exists memo_saved_at timestamptz;

create or replace function public.manshu_save_memo(p_race_key text, p_memo text)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  pk record;
  v_deadline timestamptz;
  v_memo text := btrim(coalesce(p_memo, ''));
begin
  select race_date, deadline into pk from public.manshu_chance_picks where race_key = p_race_key;
  if not found then
    return 'not_found';
  end if;
  if char_length(v_memo) > 1000 then
    raise exception 'memo must be at most 1000 characters';
  end if;
  -- 締切後は書き換えない（締切の読めないレースも書かない）
  begin
    v_deadline := (pk.race_date::text || ' ' || pk.deadline || ':00')::timestamp at time zone 'Asia/Tokyo';
  exception when others then
    v_deadline := null;
  end;
  if v_deadline is null or now() >= v_deadline then
    return 'closed';
  end if;
  if v_memo = '' then
    delete from public.manshu_memos where race_key = p_race_key;
  else
    insert into public.manshu_memos (race_key, memo, saved_at) values (p_race_key, v_memo, now())
    on conflict (race_key) do update set memo = excluded.memo, saved_at = excluded.saved_at;
  end if;
  -- 自動記録がもう済んでいれば、メモ入りの Claude 予想をその場で作り直す（起動に失敗しても保存は残す。
  -- 取りこぼしは5分おきの task=run が拾う）
  if exists (select 1 from public.manshu_source_compare where race_key = p_race_key and source = 'model') then
    begin
      perform net.http_post(
        url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/auto_post_predict.yml/dispatches',
        headers := jsonb_build_object(
          'Accept', 'application/vnd.github+json',
          'Authorization', 'Bearer ' || (
            select decrypted_secret
            from vault.decrypted_secrets
            where name = 'github_track_odds_token'
          ),
          'X-GitHub-Api-Version', '2026-03-10',
          'Content-Type', 'application/json'
        ),
        body := jsonb_build_object('ref', 'main',
                                   'inputs', jsonb_build_object('task', 'memo', 'race_key', p_race_key)),
        timeout_milliseconds := 10000
      );
    exception when others then
      null;
    end;
  end if;
  return case when v_memo = '' then 'deleted' else 'saved' end;
end;
$$;

revoke all on function public.manshu_save_memo(text, text) from public;
grant execute on function public.manshu_save_memo(text, text) to anon, authenticated, service_role;

create or replace function public.manshu_memos_for_date(p_date date)
returns table (race_key text, memo text, saved_at timestamptz, predicted_at timestamptz,
               predicted_memo_saved_at timestamptz, predict_error text)
language sql
stable
security definer
set search_path = public
as $$
  select m.race_key, m.memo, m.saved_at, c.saved_at, c.memo_saved_at, c.error
  from public.manshu_memos m
  join public.manshu_chance_picks p on p.race_key = m.race_key and p.race_date = p_date
  left join public.manshu_source_compare c on c.race_key = m.race_key and c.source = 'claude_memo';
$$;

revoke all on function public.manshu_memos_for_date(date) from public;
grant execute on function public.manshu_memos_for_date(date) to anon, authenticated, service_role;

-- 集計（列を足すので作り直す）
drop view if exists public.manshu_source_summary;

create view public.manshu_source_summary
with (security_invoker = true) as
with c as (
  select *,
         result_status = 'settled' and deadline_at is not null and saved_at < deadline_at
           and coalesce(stake, 0) > 0
           and (source <> 'claude_memo' or (memo_saved_at is not null and memo_saved_at < deadline_at)) as counted
  from public.manshu_source_compare
), a as (
  select c.*,
         count(*) filter (where counted and source in ('model', 'claude', 'blend'))
           over (partition by race_key) = 3 as all3,
         count(*) filter (where counted) over (partition by race_key) = 4 as memo4
  from c
)
select
  source,
  count(*) filter (where counted) as races,
  count(*) filter (where counted and hit) as hits,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where counted), 4) as hit_rate,
  sum(stake) filter (where counted) as stake,
  sum("return") filter (where counted) as "return",
  round(sum("return") filter (where counted)::numeric / nullif(sum(stake) filter (where counted), 0), 4) as roi,
  count(*) filter (where counted and all3) as races_all3,
  count(*) filter (where counted and all3 and hit) as hits_all3,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where counted and all3), 4) as hit_rate_all3,
  round(sum("return") filter (where counted and all3)::numeric
        / nullif(sum(stake) filter (where counted and all3), 0), 4) as roi_all3,
  round(avg(aim_count) filter (where counted), 2) as avg_points,
  round(avg(claude_listed_count) filter (where counted), 2) as avg_claude_listed,
  count(*) filter (where result_at is null and coalesce(stake, 0) > 0) as pending,
  count(*) filter (where error is not null and not (source = 'claude_memo' and memo_saved_at is null)) as errors,
  count(*) filter (where deadline_at is not null and saved_at >= deadline_at) as after_deadline,
  min(race_date) as first_date,
  max(race_date) as last_date,
  count(*) filter (where counted and memo4) as races_memo4,
  count(*) filter (where counted and memo4 and hit) as hits_memo4,
  round(avg(case when hit then 1.0 else 0.0 end) filter (where counted and memo4), 4) as hit_rate_memo4,
  sum(stake) filter (where counted and memo4) as stake_memo4,
  sum("return") filter (where counted and memo4) as return_memo4,
  round(sum("return") filter (where counted and memo4)::numeric
        / nullif(sum(stake) filter (where counted and memo4), 0), 4) as roi_memo4,
  count(*) filter (where source = 'claude_memo' and memo_saved_at is null) as no_memo
from a
group by source;

revoke all on public.manshu_source_summary from anon, authenticated;
