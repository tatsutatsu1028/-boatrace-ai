-- 万舟チャンス: 締切10分前の Discord 通知と、記録（manshu_chance_picks.aim_tickets・成績）の買い目を1つにする。
--
-- これまでの食い違い（2026-11-01 芦屋11R 20261101_21_11 など）:
--   1. 通知（notify_due_races・1分おき）は「展示後の予想が保存された（post_saved_at が入った）」時点の
--      aim_tickets を送っていた。展示後の自動予想（auto_post_predict.py）は「予想を保存 → 数秒後に
--      買い目を作り直して保存（manshu_save_aim）」の順なので、その数秒の間に通知が走ると、朝の
--      仮の買い目（展示前の確率・朝のオッズ）が通知され、記録・成績は作り直した展示後の買い目になる。
--      20261101_21_11: 予想の保存 13:31:57 → 通知 13:32:00.28 → 買い目の保存 13:32:00.52
--   2. 通知の後でも、アプリ（予想タブ・一覧の「オッズを更新」）や自動予想が締切まで買い目を
--      書き換えられた。
--
-- 直し方（買い目は1回だけ決め、通知した買い目をそのまま記録・成績に使う）:
--   - 万舟チャンスの通知は、展示後の買い目ができてから送る。条件は次のどちらか
--       a. 展示後の予想が保存され、その後に買い目が保存された（aim_saved_at >= post_saved_at）
--       b. 展示後の自動予想がそのレースを終えた（auto_post_log が done / already / skipped）
--     どちらも無ければ、今までどおり締切7分前に、その時点の買い目で送る
--   - 通知したら aim_locked_at を入れ、それ以後 manshu_save_aim は買い目を書き換えない（false を返す）。
--     通知した買い目は race_notifications.tickets にも残す（後から照合できるように）
--   - 成績（aim_hit など）・結果の通知・本日累計は今までどおり aim_tickets から作るので、通知と同じになる

alter table public.manshu_chance_picks
  add column if not exists aim_locked_at timestamptz;

alter table public.race_notifications
  add column if not exists tickets jsonb;

comment on column public.manshu_chance_picks.aim_locked_at is
  '締切前の Discord 通知で買い目を送った時刻。これ以後 aim_tickets は書き換えない（manshu_save_aim が拒否する）';
comment on column public.race_notifications.tickets is
  '万舟チャンス: 通知した中穴〜万舟狙いの買い目（送った時点の aim_tickets）';

-- 買い目の保存: 通知済み（aim_locked_at あり）なら書き換えない
create or replace function public.manshu_save_aim(
  p_race_key text, p_tickets jsonb, p_method text, p_source text, p_odds_at timestamptz)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  pk record;
  v_deadline timestamptz;
begin
  select race_date, deadline, aim_locked_at into pk from public.manshu_chance_picks where race_key = p_race_key;
  if not found then
    return false;
  end if;
  if jsonb_typeof(p_tickets) <> 'array' or jsonb_array_length(p_tickets) > 20 then
    raise exception 'tickets must be an array of at most 20';
  end if;
  -- 通知した後は書き換えない（通知・表示・記録・成績を同じ買い目にする）
  if pk.aim_locked_at is not null then
    return false;
  end if;
  -- 締切後は書き換えない（結果を見てから差し替えられないように）
  begin
    v_deadline := (pk.race_date::text || ' ' || pk.deadline || ':00')::timestamp at time zone 'Asia/Tokyo';
  exception when others then
    v_deadline := null;
  end;
  if v_deadline is not null and now() >= v_deadline then
    return false;
  end if;
  update public.manshu_chance_picks set
    aim_tickets = p_tickets,
    aim_method = left(p_method, 20),
    aim_source = left(p_source, 20),
    aim_odds_at = p_odds_at,
    aim_saved_at = now(),
    aim_error = null
  where race_key = p_race_key and result_at is null and aim_locked_at is null;
  return found;
end;
$$;

revoke all on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) from public;
grant execute on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) to anon, authenticated, service_role;

-- 締切前の通知（pg_cron notify-due-races-every-minute から1分おき）。展示前厳選10の部分は変えていない
create or replace function public.notify_due_races()
returns integer
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  r record;
  v_url text;
  v_url_manshu text;
  v_msg text;
  v_tickets text;
  v_aim jsonb;
  v_req bigint;
  v_n int := 0;
  v_today date := (now() at time zone 'Asia/Tokyo')::date;
begin
  select decrypted_secret into v_url
  from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  select decrypted_secret into v_url_manshu
  from vault.decrypted_secrets where name = 'discord_webhook_manshu' limit 1;
  v_url_manshu := coalesce(v_url_manshu, v_url);
  if v_url is null then return 0; end if;

  for r in
    select * from (
      select 'pre'::text as src, race_key, venue, race_no, deadline, pick_rank, post_saved_at,
             coalesce(post_tickets, tickets) as tk, null::jsonb as ls,
             post_flag, post_flag_reasons::text[] as reasons,
             (race_date::text || ' ' || deadline || ':00+09')::timestamptz as dl,
             (post_saved_at is not null) as ready
      from public.pre_exhibition_picks
      where race_date = v_today and deadline ~ '^\d{1,2}:\d{2}$'
      union all
      select 'manshu', p.race_key, p.venue, p.race_no, p.deadline, p.pick_rank, p.post_saved_at,
             coalesce(p.post_tickets, p.tickets),
             coalesce(p.post_longshot_tickets, p.longshot_tickets),
             p.post_flag, p.post_flag_reasons::text[],
             (p.race_date::text || ' ' || p.deadline || ':00+09')::timestamptz,
             -- 展示後の買い目ができてから送る（予想の保存と買い目の保存の間に送らない）
             -- post_saved_at は text（ISO の日時）。読めない値なら a は使わない
             coalesce(p.aim_saved_at >= case when p.post_saved_at ~ '^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}'
                                             then p.post_saved_at::timestamptz end, false)
             or exists (select 1 from public.auto_post_log l
                        where l.race_key = p.race_key and l.status in ('done', 'already', 'skipped'))
      from public.manshu_chance_picks p
      where p.race_date = v_today and p.deadline ~ '^\d{1,2}:\d{2}$'
    ) c
    where now() < c.dl
      and (c.ready or now() >= c.dl - interval '7 minutes')
      and not exists (select 1 from public.race_notifications n
                      where n.src = c.src and n.race_key = c.race_key)
    order by c.dl
  loop
    v_tickets := null;
    v_aim := null;
    if r.src = 'pre' then
      v_msg := format('🎯 **厳選#%s｜%s %sR｜締切 %s**（%s）',
                      r.pick_rank, r.venue, r.race_no, r.deadline,
                      case when r.post_saved_at is not null then '展示後' else '展示前・展示後未取得' end);
      select string_agg(g.line, E'\n') into v_tickets from (
        select grp || ': ' || string_agg((t->>'combo') || ' ' || (t->>'stake') || '円', ' / ') as line,
               min(case grp when '本線' then 1 else 2 end) as ord
        from (select t, coalesce(t->>'group', '買い目') as grp
              from jsonb_array_elements(coalesce(r.tk, '[]'::jsonb)) t
              where coalesce((t->>'stake')::numeric, 0) > 0) s
        group by grp order by ord
      ) g;
    else
      -- 送る買い目を固定する（ここで aim_locked_at を入れ、以後の manshu_save_aim は書き換えない）。
      -- 行ロックで、同時に走る manshu_save_aim の前後どちらか一方の買い目だけを読む
      update public.manshu_chance_picks set aim_locked_at = now()
      where race_key = r.race_key and aim_locked_at is null;
      select aim_tickets into v_aim from public.manshu_chance_picks where race_key = r.race_key for update;
      v_msg := format('💰 **万舟#%s｜%s %sR｜締切 %s**（%s）',
                      r.pick_rank, r.venue, r.race_no, r.deadline,
                      case when r.post_saved_at is not null then '展示後' else '展示前・展示後未取得' end);
      if v_aim is not null and jsonb_array_length(v_aim) > 0 then
        select string_agg(g.line, E'\n') into v_tickets from (
          select tier || ': ' || string_agg((t->>'combo') || '(' || (t->>'odds') || '倍)', ' / ') as line,
                 min(case tier when '中穴' then 1 else 2 end) as ord
          from (select t, coalesce(t->>'tier', '買い目') as tier
                from jsonb_array_elements(v_aim) t) s
          group by tier order by ord
        ) g;
      else
        select '穴目: ' || string_agg(t->>'combo', ' / ') into v_tickets
        from jsonb_array_elements(coalesce(r.ls, '[]'::jsonb)) t;
      end if;
    end if;

    v_msg := v_msg || E'\n' || coalesce(v_tickets, '（買い目なし）');
    if coalesce(r.post_flag, false) then
      v_msg := v_msg || E'\n⚠️ 展示後注意: ' || coalesce(array_to_string(r.reasons, '、'), '');
    end if;

    insert into public.race_notifications (src, race_key, basis, deadline_at, tickets)
    values (r.src, r.race_key,
            case when r.post_saved_at is not null then 'post' else 'pre' end, r.dl, v_aim)
    on conflict do nothing;
    if not found then continue; end if;

    select net.http_post(
      url := case when r.src = 'manshu' then v_url_manshu else v_url end,
      body := jsonb_build_object('content', left(v_msg, 1900))
    ) into v_req;
    update public.race_notifications set request_id = v_req
    where src = r.src and race_key = r.race_key;
    v_n := v_n + 1;
  end loop;
  return v_n;
end;
$function$;

-- 点検用: 通知した買い目と記録（aim_tickets）が違うレース（このマイグレーションの後は0件のはず）
create or replace view public.manshu_aim_notify_mismatch
with (security_invoker = true) as
select p.race_date, p.race_key, p.venue, p.race_no, n.sent_at, p.aim_saved_at, p.aim_source,
       n.tickets as notified_tickets, p.aim_tickets as recorded_tickets
from public.manshu_chance_picks p
join public.race_notifications n on n.src = 'manshu' and n.race_key = p.race_key
where n.tickets is not null
  and (select array_agg(t->>'combo' order by t->>'combo') from jsonb_array_elements(n.tickets) t)
      is distinct from
      (select array_agg(t->>'combo' order by t->>'combo') from jsonb_array_elements(coalesce(p.aim_tickets, '[]'::jsonb)) t);

revoke all on public.manshu_aim_notify_mismatch from anon, authenticated;
