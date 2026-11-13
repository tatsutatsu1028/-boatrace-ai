-- オリジナル展示の記録の対象に展示前厳選10（pre_exhibition_picks）を足すのに合わせ、
-- どちらの対象として取ったかを original_exhibition_log に残す。何も削除しない。
--
--   target_kind : manshu（万舟チャンスだけ）/ pick（厳選10だけ）/ both（両方。取るのは1回）
--
-- これまでの記録は万舟チャンスのレースだけなので、厳選10にも入っていれば both、それ以外は manshu で埋める。

alter table public.original_exhibition_log
  add column if not exists target_kind text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'original_exhibition_log_target_kind_check') then
    alter table public.original_exhibition_log add constraint original_exhibition_log_target_kind_check
      check (target_kind is null or target_kind in ('manshu', 'pick', 'both'));
  end if;
end $$;

update public.original_exhibition_log l
set target_kind = case
  when exists (select 1 from public.pre_exhibition_picks p where p.race_key = l.race_key) then 'both'
  else 'manshu' end
where l.target_kind is null;
