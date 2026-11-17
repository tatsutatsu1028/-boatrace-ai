-- 厳選10（朝一厳選レース）の結果通知に「本線で的中したか」を出す（2026-11-17〜）。
--
--   notify_pre_results : 的中のとき、当たった買い目とその区分（本線／抑え）を出す
--                          例: ✅ **的中！本線｜厳選#3 江戸川 4R**
--                              的中 1-4-2（本線・600円）
--                        本日累計にも本線での的中数を出す（例: 本日累計 8戦5的中（本線3）｜…）
--                        区分は通知に使った買い目（展示後なら post_tickets、展示前なら tickets）の group で見る。
--                        それ以外（送り先・返還・投資/払戻の出し方）は今までどおり。

create or replace function public.notify_pre_results()
returns integer
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  r record;
  v_url text;
  v_msg text;
  v_req bigint;
  v_n int := 0;
  v_today date := (now() at time zone 'Asia/Tokyo')::date;
  v_stake bigint; v_ret bigint; v_cnt int; v_hits int; v_honsen int;
  v_tk jsonb;
  v_grp text;
  v_hit_stake numeric;
begin
  select decrypted_secret into v_url
  from vault.decrypted_secrets where name = 'discord_webhook_pre_result' limit 1;
  if v_url is null then return 0; end if;

  for r in
    select p.*,
           case when coalesce(n.basis, case when p.post_stake is not null then 'post' else 'pre' end) = 'post'
                     and p.post_stake is not null then 'post' else 'pre' end as use_basis
    from public.pre_exhibition_picks p
    left join public.race_notifications n on n.src = 'pre' and n.race_key = p.race_key
    where p.race_date = v_today
      and p.result_status in ('settled', 'void')
      and not exists (select 1 from public.race_notifications x
                      where x.src = 'pre_result' and x.race_key = p.race_key)
    order by p.result_at nulls last
  loop
    insert into public.race_notifications (src, race_key, basis)
    values ('pre_result', r.race_key, r.use_basis)
    on conflict do nothing;
    if not found then continue; end if;

    if r.result_status = 'void' then
      v_msg := format('↩️ **返還｜厳選#%s %s %sR**', r.pick_rank, r.venue, r.race_no);
    else
      if (case when r.use_basis = 'post' then r.post_hit else r.pre_hit end) then
        -- 当たった買い目の区分（本線／抑え）
        v_grp := null; v_hit_stake := null;
        v_tk := case when r.use_basis = 'post' then coalesce(r.post_tickets, r.tickets) else r.tickets end;
        select coalesce(t->>'group', '買い目'), (t->>'stake')::numeric
          into v_grp, v_hit_stake
        from jsonb_array_elements(coalesce(v_tk, '[]'::jsonb)) t
        where t->>'combo' = r.trifecta_actual
          and coalesce((t->>'stake')::numeric, 0) > 0
        order by case coalesce(t->>'group', '') when '本線' then 1 else 2 end
        limit 1;
        if v_grp = '本線' then
          v_msg := format('✅ **的中！本線｜厳選#%s %s %sR**', r.pick_rank, r.venue, r.race_no);
        elsif v_grp is not null then
          v_msg := format('✅ **的中（%s）｜厳選#%s %s %sR**', v_grp, r.pick_rank, r.venue, r.race_no);
        else
          v_msg := format('✅ **的中！厳選#%s %s %sR**', r.pick_rank, r.venue, r.race_no);
        end if;
        if v_grp is not null then
          v_msg := v_msg || format(E'\n的中 %s（%s・%s円）', r.trifecta_actual, v_grp,
                                   to_char(coalesce(v_hit_stake, 0), 'FM999,999,990'));
        end if;
      else
        v_msg := format('❌ ハズレ｜厳選#%s %s %sR', r.pick_rank, r.venue, r.race_no);
      end if;
      v_msg := v_msg || format(E'\n結果 %s（%s円）', coalesce(r.trifecta_actual, '-'),
                 to_char(coalesce(r.trifecta_payout, 0), 'FM999,999,990'));
      v_msg := v_msg || format(E'\n投資 %s円 → 払戻 %s円（%s）',
                 to_char(coalesce(case when r.use_basis = 'post' then r.post_stake else r.pre_stake end, 0), 'FM999,999,990'),
                 to_char(coalesce(case when r.use_basis = 'post' then r.post_return else r.pre_return end, 0), 'FM999,999,990'),
                 case when r.use_basis = 'post' then '展示後' else '展示前' end);
    end if;

    -- 本日累計（確定分）。本線での的中数も数える
    select count(*),
           count(*) filter (where case when b.post_stake is not null then b.post_hit else b.pre_hit end),
           count(*) filter (where (case when b.post_stake is not null then b.post_hit else b.pre_hit end)
                              and exists (select 1 from jsonb_array_elements(coalesce(
                                            case when b.post_stake is not null then coalesce(b.post_tickets, b.tickets)
                                                 else b.tickets end, '[]'::jsonb)) t
                                          where t->>'combo' = b.trifecta_actual
                                            and t->>'group' = '本線'
                                            and coalesce((t->>'stake')::numeric, 0) > 0)),
           sum(coalesce(case when b.post_stake is not null then b.post_stake else b.pre_stake end, 0)),
           sum(coalesce(case when b.post_stake is not null then b.post_return else b.pre_return end, 0))
      into v_cnt, v_hits, v_honsen, v_stake, v_ret
    from public.pre_exhibition_picks b
    where b.race_date = v_today and b.result_status = 'settled';
    v_msg := v_msg || format(E'\n本日累計 %s戦%s的中（本線%s）｜投資 %s円 / 払戻 %s円（回収率 %s%%）',
               v_cnt, v_hits, v_honsen,
               to_char(coalesce(v_stake, 0), 'FM999,999,990'),
               to_char(coalesce(v_ret, 0), 'FM999,999,990'),
               case when coalesce(v_stake, 0) > 0 then round(100.0 * v_ret / v_stake, 1)::text else '-' end);

    select net.http_post(
      url := v_url,
      body := jsonb_build_object('content', left(v_msg, 1900))
    ) into v_req;
    update public.race_notifications set request_id = v_req
    where src = 'pre_result' and race_key = r.race_key;
    v_n := v_n + 1;
  end loop;
  return v_n;
end;
$function$;
