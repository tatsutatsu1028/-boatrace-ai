-- 万舟チャンス: 頭本命（万舟狙い用の1着軸）を表示・記録する。買い目の作り方は変えない（表示と記録だけ）。
--
--   頭本命 = 1着確率が2番目に高い艇（同じ確率なら内側）。本命（favorite_lane）と同じく
--   展示後の確率（post_tri_probs を1着の艇で足したもの）があればそれ、無ければ展示前の p_first で決める。
--
--   head_axis_lane / head_axis_prob           : 展示前（朝の pre_picks.py が本命と同じときに保存）
--   post_head_axis_lane / post_head_axis_prob : 展示後（manshu_pick_apply_post が post_tri_probs から書く）
--
-- 既存のレースは後埋めしない（p_first・post_tri_probs から後で計算できる）。
-- 締切前の Discord 通知（notify_due_races）に「頭本命：○号艇」の1行を足す。それ以外は 20261101000000 と同じ。

alter table public.manshu_chance_picks
  add column if not exists head_axis_lane smallint,
  add column if not exists head_axis_prob real,
  add column if not exists post_head_axis_lane smallint,
  add column if not exists post_head_axis_prob real;

comment on column public.manshu_chance_picks.head_axis_lane is
  '頭本命（展示前）: 展示前の p_first で1着確率が2番目に高い艇（同じなら内側）。表示・検証用で買い目には使わない';
comment on column public.manshu_chance_picks.post_head_axis_lane is
  '頭本命（展示後）: post_tri_probs を1着の艇で足した確率が2番目に高い艇（同じなら内側）';

create or replace function public.manshu_pick_apply_post(p_race_key text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  pk public.manshu_chance_picks%rowtype;
  s record;
  p jsonb;
  mp jsonb;
  v_t jsonb;
  v_l jsonb;
  v_tri jsonb;
  v_fav smallint;
  v_head smallint;
  v_head_p real;
  v_mp real;
  reasons text[] := '{}';
begin
  select * into pk from public.manshu_chance_picks where race_key = p_race_key;
  if not found then
    return;
  end if;
  select * into s from public.prediction_snapshots where race_key = p_race_key
    order by saved_at desc limit 1;
  if not found then
    return;
  end if;
  p := s.payload_json::jsonb;
  mp := p -> 'prediction_model' -> 'manshu_post';
  select jsonb_agg(jsonb_build_object(
           'combo', t ->> 'combo', 'group', t ->> 'group',
           'prob', (t ->> 'prob')::float8, 'stake', coalesce((t ->> 'stake')::numeric, 0)::int))
    into v_t
    from jsonb_array_elements(p -> 'tickets') t;
  if jsonb_typeof(mp -> 'longshot') = 'array' and jsonb_array_length(mp -> 'longshot') > 0 then
    v_l := mp -> 'longshot';
  end if;
  if jsonb_typeof(mp -> 'tri') = 'object' and mp -> 'tri' <> '{}'::jsonb then
    v_tri := mp -> 'tri';
  end if;
  select (e ->> 'lane')::numeric::smallint into v_fav
    from jsonb_array_elements(p -> 'final') e
    order by (e ->> 'p_first')::float8 desc nulls last limit 1;
  -- 頭本命（展示後）: 展示後の3連単確率を1着の艇で足し、2番目に高い艇（同じなら内側）。3連単確率が無ければ書かない
  if v_tri is not null then
    select x.lane, x.p into v_head, v_head_p
      from (select left(t.key, 1)::smallint as lane, sum((t.value #>> '{}')::float8)::real as p
              from jsonb_each(v_tri) t
             where left(t.key, 1) ~ '^[1-6]$'
             group by 1) x
     order by x.p desc, x.lane asc
     offset 1 limit 1;
  end if;
  v_mp := (mp ->> 'prob')::real;
  if v_fav is not null and pk.favorite_lane is not null and v_fav is distinct from pk.favorite_lane then
    reasons := reasons || format('本命 %s→%s号艇', pk.favorite_lane, v_fav);
  end if;
  if v_mp is not null and v_mp <= pk.manshu_prob - 0.10 then
    reasons := reasons || format('万舟確率 %s%%→%s%%', round(pk.manshu_prob * 100), round(v_mp * 100));
  end if;
  update public.manshu_chance_picks set
    post_manshu_prob = v_mp,
    post_manshu_model_version = mp ->> 'version',
    post_model_version = nullif(coalesce(p ->> 'model_version', ''), ''),
    post_tickets = v_t,
    post_longshot_tickets = v_l,
    post_tri_probs = v_tri,
    post_favorite_lane = v_fav,
    post_head_axis_lane = v_head,
    post_head_axis_prob = v_head_p,
    post_flag = cardinality(reasons) > 0,
    post_flag_reasons = reasons,
    post_saved_at = s.saved_at,
    post_pre_exhibition = s.pre_exhibition
  where race_key = p_race_key;
end;
$$;

revoke all on function public.manshu_pick_apply_post(text) from public, anon, authenticated;

-- 締切前の通知（pg_cron notify-due-races-every-minute から1分おき）。万舟チャンスに「頭本命：○号艇」の行を足した
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

    if r.src = 'manshu' and r.head is not null then
      v_msg := v_msg || E'\n頭本命：' || r.head || '号艇';
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

