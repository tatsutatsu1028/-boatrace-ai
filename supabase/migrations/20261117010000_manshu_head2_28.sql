-- 万舟チャンスの買い目を「頭2艇・28点」（HEAD2-NB2-28。manshu.head2_tickets）にする（2026-11-17〜）。
--
--   manshu_save_aim   : 買い目の点数の上限を 20 → 30 にする（28点を保存できるように）。それ以外は 20261101000000 と同じ
--   notify_due_races  : 万舟チャンスの通知で、28点の買い目は頭ごとに1行
--                       （例: 頭1 3号艇: 3-45-全／3-全-45（14点））。前の作り方の買い目（中穴・万舟）は今までどおり。
--                       それ以外は 20261108020000_note_morning.sql と同じ
--
-- 的中・投資・払戻（aim_hit / aim_stake / aim_return）は今までどおり保存した買い目（aim_tickets）で集計する。
-- aim_method は 'HEAD2-NB2-28'（前の作り方は 'MIX5+5-prob-no1'）。

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
  if jsonb_typeof(p_tickets) <> 'array' or jsonb_array_length(p_tickets) > 30 then
    raise exception 'tickets must be an array of at most 30';
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
  v_note_on boolean := false;
  v_base text;
  v_key text;
  v_link text;
  v_scope text := 'pre';
  v_note_row boolean;
  v_mode text := 'post';
begin
  select decrypted_secret into v_url
  from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  select decrypted_secret into v_url_manshu
  from vault.decrypted_secrets where name = 'discord_webhook_manshu' limit 1;
  v_url_manshu := coalesce(v_url_manshu, v_url);
  if v_url is null then return 0; end if;
  select coalesce(s.note_auto_enabled, false), nullif(trim(s.note_view_base_url), ''),
         coalesce(s.note_auto_scope, 'pre'), coalesce(s.note_mode, 'post')
    into v_note_on, v_base, v_scope, v_mode
  from public.app_settings s where s.id = 1;
  v_scope := coalesce(v_scope, 'pre');
  v_mode := coalesce(v_mode, 'post');
  v_note_on := coalesce(v_note_on, false);
  if v_note_on then
    select decrypted_secret into v_key from vault.decrypted_secrets where name = 'note_view_key' limit 1;
  end if;

  for r in
    select c.*, l.note_status, l.note_reason from (
      select 'pre'::text as src, race_key, venue, race_no, deadline, pick_rank, post_saved_at,
             coalesce(post_tickets, tickets) as tk, null::jsonb as ls,
             post_flag, post_flag_reasons::text[] as reasons,
             (race_date::text || ' ' || deadline || ':00+09')::timestamptz as dl,
             (post_saved_at is not null) as ready,
             null::smallint as head
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
                        where l.race_key = p.race_key and l.status in ('done', 'already', 'skipped')),
             -- 頭本命（1着確率が2番目に高い艇）: 展示後があれば展示後、無ければ朝。
             -- 列ができる前に保存した行は朝の p_first から出す（同じ確率なら内側）
             coalesce(p.post_head_axis_lane, p.head_axis_lane,
                      (select i::smallint from generate_subscripts(p.p_first, 1) i
                        where p.p_first[i] is not null
                        order by p.p_first[i] desc, i asc offset 1 limit 1))
      from public.manshu_chance_picks p
      where p.race_date = v_today and p.deadline ~ '^\d{1,2}:\d{2}$'
    ) c
    left join public.auto_post_log l on l.race_key = c.race_key
    where now() < c.dl
      and (c.ready or now() >= c.dl - interval '7 minutes')
      -- 自動下書きが ON: 記事ができるまで待つ（展示後の予想の保存から最大3分・締切7分前まで）
      and (not v_note_on
           -- 対象が「厳選のみ」なら、万舟チャンスの通知は記事を待たない
           or (c.src = 'manshu' and v_scope = 'pre')
           -- 朝の記事＋自動更新: 厳選10の通知は記事を待たない（展示後の記事を作らないため）
           or (c.src = 'pre' and v_mode = 'morning')
           or l.note_status is not null
           or now() >= c.dl - interval '7 minutes'
           or now() >= coalesce(case when c.post_saved_at ~ '^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}'
                                     then c.post_saved_at::timestamptz end, now()) + interval '3 minutes')
      and not exists (select 1 from public.race_notifications n
                      where n.src = c.src and n.race_key = c.race_key)
    order by c.dl
  loop
    v_tickets := null;
    v_aim := null;
    -- この通知に記事の行を載せるか（対象が「厳選のみ」なら万舟チャンスの通知には載せない）
    v_note_row := v_note_on and (r.src <> 'manshu' or v_scope = 'pre_manshu') and (r.src <> 'pre' or v_mode <> 'morning');
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
      if v_aim is not null and jsonb_array_length(v_aim) > 0
         and exists (select 1 from jsonb_array_elements(v_aim) t where t ? 'head') then
        -- 頭2艇・28点（HEAD2-NB2-28）: 頭ごとに1行（例: 頭1 3号艇: 3-45-全／3-全-45（14点））
        select string_agg(format('頭%s %s号艇: %s-%s%s-全／%s-全-%s%s（%s点）',
                                 x.rk, x.h, x.h, (x.h % 6) + 1, ((x.h + 1) % 6) + 1,
                                 x.h, (x.h % 6) + 1, ((x.h + 1) % 6) + 1, x.n), E'\n' order by x.rk)
          into v_tickets
        from (select coalesce((t->>'head_rank')::int, 0) as rk, (t->>'head')::int as h, count(*) as n
              from jsonb_array_elements(v_aim) t
              where t ? 'head'
              group by 1, 2) x;
      elsif v_aim is not null and jsonb_array_length(v_aim) > 0 then
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

    if r.src = 'manshu' and r.head is not null then
      v_msg := v_msg || E'\n頭本命：' || r.head || '号艇';
    end if;
    v_msg := v_msg || E'\n' || coalesce(v_tickets, '（買い目なし）');
    if coalesce(r.post_flag, false) then
      v_msg := v_msg || E'\n⚠️ 展示後注意: ' || coalesce(array_to_string(r.reasons, '、'), '');
    end if;

    -- note の自動下書き（URL は <> で囲み、Discord のリンクのプレビューを出さない）
    if v_note_row then
      v_link := case when v_base is not null and v_key is not null
                     then '<' || rtrim(v_base, '/') || '/?view=note&race_key=' || r.race_key || '&key=' || v_key || '>'
                     else '（記事ページのURLが未設定：設定タブで登録）' end;
      v_msg := v_msg || E'\n' || case
        when r.note_status = 'ok' then '📝 note記事（自動下書き）: ' || v_link
        when r.note_status = 'manual' then '📝 note記事（手動で確定済み）: ' || v_link
        when r.note_status = 'template' then '⚠️ 記事生成失敗（Claudeの本文を作れず、買い目だけの型で保存: '
                             || left(coalesce(r.note_reason, ''), 80) || '）' || E'\n📝 ' || v_link
        when r.note_status = 'failed' then '⚠️ 記事生成失敗: ' || left(coalesce(r.note_reason, ''), 120)
        when r.post_saved_at is null then '📝 note記事はまだありません（展示後の予想待ち。できたら追いかけで通知します）'
        else '⚠️ 記事生成失敗（通知までに記事ができませんでした。後でできたら追いかけで通知します）'
      end;
    end if;

    insert into public.race_notifications (src, race_key, basis, deadline_at, tickets, note_sent)
    values (r.src, r.race_key,
            case when r.post_saved_at is not null then 'post' else 'pre' end, r.dl, v_aim,
            case when v_note_row then r.note_status is not null end)
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

  -- 記事を載せられずに送った通知の追いかけ（記事ができたら締切前に1回だけ）
  if v_note_on then
    for r in
      select n.src, n.race_key, n.deadline_at, l.venue, l.race_no, l.deadline, l.note_status, l.note_reason
      from public.race_notifications n
      join public.auto_post_log l on l.race_key = n.race_key
      where n.note_sent = false and n.note_followup_at is null
        and (n.src <> 'manshu' or v_scope = 'pre_manshu')
        and (n.src <> 'pre' or v_mode <> 'morning')
        and l.note_status is not null and now() < n.deadline_at
        and n.deadline_at >= now() - interval '1 day'
      order by n.deadline_at
    loop
      v_link := case when v_base is not null and v_key is not null
                     then '<' || rtrim(v_base, '/') || '/?view=note&race_key=' || r.race_key || '&key=' || v_key || '>'
                     else '（記事ページのURLが未設定：設定タブで登録）' end;
      v_msg := format('%s **%s %sR｜締切 %s** ', case when r.src = 'manshu' then '💰' else '🎯' end,
                      r.venue, r.race_no, r.deadline)
        || case
             when r.note_status = 'ok' then E'の note記事ができました（展示後の予想で作成）\n📝 ' || v_link
             when r.note_status = 'manual' then E'の note記事（手動で確定済み）\n📝 ' || v_link
             when r.note_status = 'template' then E'の note記事: ⚠️ 記事生成失敗（Claudeの本文を作れず、買い目だけの型で保存: '
                                                  || left(coalesce(r.note_reason, ''), 80) || E'）\n📝 ' || v_link
             else 'の note記事: ⚠️ 記事生成失敗: ' || left(coalesce(r.note_reason, ''), 120)
           end;
      update public.race_notifications set note_followup_at = now()
      where src = r.src and race_key = r.race_key and note_followup_at is null;
      if not found then continue; end if;
      select net.http_post(
        url := case when r.src = 'manshu' then v_url_manshu else v_url end,
        body := jsonb_build_object('content', left(v_msg, 1900))
      ) into v_req;
      update public.race_notifications set note_followup_request_id = v_req
      where src = r.src and race_key = r.race_key;
    end loop;
  end if;
  return v_n;
end;
$function$;

