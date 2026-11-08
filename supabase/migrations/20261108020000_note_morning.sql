-- note を「朝に展示前の記事を公開し、展示後の買い目はリンク先（購入者向けページ）で自動更新する」形にする（厳選10だけ）。
-- 何も削除しない。
--
--   app_settings.note_mode : post = 展示後の記事（今まで通り・既定）/ morning = 朝の記事＋自動更新
--   note_publications に朝の記事の列を足す:
--     article_mode   : 'morning'（朝の記事）。今までの記事は null
--     buyer_token    : 購入者向けページのレースごとの乱数（マスターキーとは別。そのレースしか見られない）
--     pre_text       : 展示前の予想展開と買い目（購入者向けページ用）、pre_tickets: 展示前の買い目
--     post_status    : pending（展示後の更新待ち）/ updated（更新済み）/ none（更新なし）
--     post_text・post_tickets・post_updated_at : 展示後の予想展開と買い目・更新時刻
--     展示後に更新できたら article_tickets を展示後の買い目にする（夜のまとめ・結果報告の的中判定がそれを使う）。
--     更新できなかったレースは展示前の買い目のまま
--   note_morning_save        : 朝の記事を保存（service_role だけ。note_morning.py）。本文の {{BUYER_URL}} を購入者向けページの URL にする
--   note_morning_post_update : 展示後の更新・更新なしを記録（service_role だけ。auto_post_predict.py）
--   note_buyer_view          : 購入者向けページ（?view=buy&r=…&t=…）。レースと乱数が合うときだけ、そのレースを返す
--   note_morning_notify      : 「🌅 朝の記事N本ができました」と朝の一覧ページの URL を厳選10のチャンネルに送る（service_role だけ）
--   note_view_articles_v3    : v2 に朝の記事の列を足し、p_morning = true でその日の朝の記事だけを返す
--   note_auto_save_draft     : 朝の記事の行は書き換えない（manual を返す）
--   notify_due_races         : 朝の記事＋自動更新のときは、厳選10の通知は記事を待たず記事の行を載せない
--   pg_cron note-morning     : 毎朝 JST 9:02 に note_morning.yml を起動（厳選10の確定を待って作る。OFF の日は何もしない）

alter table public.app_settings
  add column if not exists note_mode text not null default 'post';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'app_settings_note_mode_check') then
    alter table public.app_settings add constraint app_settings_note_mode_check check (note_mode in ('post', 'morning'));
  end if;
end $$;

alter table public.note_publications
  add column if not exists article_mode text,
  add column if not exists buyer_token text,
  add column if not exists pre_text text,
  add column if not exists pre_tickets jsonb,
  add column if not exists post_status text,
  add column if not exists post_text text,
  add column if not exists post_tickets jsonb,
  add column if not exists post_updated_at timestamptz;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'note_publications_post_status_check') then
    alter table public.note_publications add constraint note_publications_post_status_check
      check (post_status is null or post_status in ('pending', 'updated', 'none'));
  end if;
end $$;

create unique index if not exists note_publications_buyer_token_key
  on public.note_publications (buyer_token) where buyer_token is not null;

create or replace function public.note_morning_save(
  p_race_key text, p_publication_date date, p_venue text, p_race_no integer, p_title text, p_article_text text,
  p_pre_text text, p_tickets jsonb, p_article_writer text, p_p1_lane integer, p_p1_prob double precision,
  p_price integer default 300
)
returns text
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  ex public.note_publications;
  v_seq integer;
  v_token text;
  v_base text;
  v_url text;
  v_exists boolean;
begin
  select * into ex from public.note_publications where race_key = p_race_key for update;
  v_exists := found;
  if v_exists and not (ex.article_mode = 'morning' and ex.status = 'AUTO_DRAFT') then
    return 'exists';
  end if;
  v_token := coalesce(ex.buyer_token, encode(extensions.gen_random_bytes(16), 'hex'));
  select nullif(trim(note_view_base_url), '') into v_base from public.app_settings where id = 1;
  v_url := case when v_base is null then '（URL未設定）'
                else rtrim(v_base, '/') || '/?view=buy&r=' || p_race_key || '&t=' || v_token end;
  if v_exists then
    update public.note_publications set
      title = p_title, article_text = replace(p_article_text, '{{BUYER_URL}}', v_url), pre_text = p_pre_text,
      pre_tickets = p_tickets, article_tickets = case when post_status = 'updated' then article_tickets else p_tickets end,
      article_writer = p_article_writer, p1_lane = p_p1_lane, p1_prob = p_p1_prob, price = coalesce(p_price, 300),
      created_at = now()
    where id = ex.id;
    return 'updated';
  end if;
  perform pg_advisory_xact_lock(hashtext(p_publication_date::text));
  select coalesce(min(sequence_no), 0) - 1 into v_seq
  from public.note_publications where publication_date = p_publication_date and sequence_no < 0;
  insert into public.note_publications (
    race_key, publication_date, venue, race_no, sequence_no, publication_type, price, trigger_reason,
    p1_lane, p1_prob, title, article_text, status, ticket_source, article_tickets, article_writer,
    article_mode, buyer_token, pre_text, pre_tickets, post_status
  ) values (
    p_race_key, p_publication_date, p_venue, p_race_no, v_seq, 'PAID', coalesce(p_price, 300),
    '朝の記事（展示前）', p_p1_lane, p_p1_prob, p_title, replace(p_article_text, '{{BUYER_URL}}', v_url), 'AUTO_DRAFT',
    'model', p_tickets, p_article_writer, 'morning', v_token, p_pre_text, p_tickets, 'pending'
  )
  on conflict (race_key) do nothing;
  if not found then
    return 'exists';
  end if;
  return 'created';
end;
$function$;

create or replace function public.note_morning_post_update(
  p_race_key text, p_status text, p_post_text text, p_post_tickets jsonb
)
returns text
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
  if p_status not in ('updated', 'none') then
    return 'invalid';
  end if;
  update public.note_publications set
    post_status = p_status,
    post_text = case when p_status = 'updated' then p_post_text else post_text end,
    post_tickets = case when p_status = 'updated' then p_post_tickets else post_tickets end,
    article_tickets = case when p_status = 'updated' and p_post_tickets is not null then p_post_tickets else article_tickets end,
    post_updated_at = now()
  where race_key = p_race_key and article_mode = 'morning'
    -- 一度更新したレースを「更新なし」で上書きしない
    and not (p_status = 'none' and post_status = 'updated');
  return case when found then p_status else 'not_found' end;
end;
$function$;

create or replace function public.note_buyer_view(p_race_key text, p_token text)
returns table (venue text, race_no integer, deadline text, publication_date date, pre_text text,
               post_status text, post_text text, post_updated_at timestamptz)
language sql
stable
security definer
set search_path to 'public'
as $function$
  select n.venue, n.race_no,
         coalesce((select p.deadline from public.pre_exhibition_picks p where p.race_key = n.race_key limit 1), '') as deadline,
         n.publication_date, n.pre_text, n.post_status,
         case when n.post_status = 'updated' then n.post_text end, n.post_updated_at
  from public.note_publications n
  where length(coalesce(p_token, '')) >= 32 and n.article_mode = 'morning'
    and n.race_key = p_race_key and n.buyer_token = p_token;
$function$;

create or replace function public.note_morning_notify(p_count integer, p_date date)
returns integer
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  v_hook text;
  v_base text;
  v_key text;
begin
  select decrypted_secret into v_hook from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  select decrypted_secret into v_key from vault.decrypted_secrets where name = 'note_view_key' limit 1;
  select nullif(trim(note_view_base_url), '') into v_base from public.app_settings where id = 1;
  if v_hook is null then
    return 0;
  end if;
  perform net.http_post(url := v_hook, body := jsonb_build_object('content', format(
    E'🌅 朝の記事%s本ができました\n%s', p_count,
    case when v_base is null or v_key is null then '（記事ページのURLが未設定：設定タブで登録）'
         else '<' || rtrim(v_base, '/') || '/?view=note&morning=' || p_date || '&key=' || v_key || '>' end)));
  return 1;
end;
$function$;

create or replace function public.note_view_articles_v3(p_key text, p_race_key text default null, p_date date default null,
                                                        p_morning boolean default false)
returns table (
  race_key text, publication_date date, venue text, race_no integer, title text, article_text text,
  price integer, status text, ticket_source text, article_writer text, created_at timestamptz, deadline text,
  sequence_no integer, header_image text, header_image_reason text, note_url text, published_at timestamptz,
  is_manshu boolean, is_pre boolean, article_mode text, post_status text, post_updated_at timestamptz
)
language sql
stable
security definer
set search_path to 'public'
as $function$
  select n.race_key, n.publication_date, n.venue, n.race_no, n.title, n.article_text,
         n.price, n.status, n.ticket_source, n.article_writer, n.created_at,
         coalesce((select p.deadline from public.pre_exhibition_picks p where p.race_key = n.race_key limit 1),
                  (select m.deadline from public.manshu_chance_picks m where m.race_key = n.race_key limit 1)) as deadline,
         n.sequence_no, n.header_image, n.header_image_reason, n.note_url, n.published_at,
         exists (select 1 from public.manshu_chance_picks m where m.race_key = n.race_key) as is_manshu,
         exists (select 1 from public.pre_exhibition_picks p where p.race_key = n.race_key) as is_pre,
         n.article_mode, n.post_status, n.post_updated_at
  from public.note_publications n
  where public.note_view_key_ok(p_key)
    and (case when nullif(p_race_key, '') is not null then n.race_key = p_race_key
              when coalesce(p_morning, false) then n.article_mode = 'morning'
                   and n.publication_date = coalesce(p_date, (now() at time zone 'Asia/Tokyo')::date)
              else n.publication_date = coalesce(p_date, (now() at time zone 'Asia/Tokyo')::date)
                   and (n.status = 'AUTO_DRAFT' or n.trigger_reason in ('自動下書き（締切前の通知）', '朝の記事（展示前）')) end)
  order by deadline nulls last, n.race_key;
$function$;

-- 自動下書き（展示後の記事）は、朝の記事の行を書き換えない
create or replace function public.note_auto_save_draft(
  p_race_key text, p_publication_date date, p_venue text, p_race_no integer,
  p_title text, p_article_text text, p_ticket_source text, p_article_tickets jsonb, p_article_writer text,
  p_p1_lane integer, p_p1_prob double precision, p_price integer default 300
)
returns text
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  ex public.note_publications;
  v_seq integer;
begin
  select * into ex from public.note_publications where race_key = p_race_key for update;
  if found then
    if ex.status <> 'AUTO_DRAFT' or ex.article_mode = 'morning' then
      return 'manual';
    end if;
    update public.note_publications set
      title = p_title, article_text = p_article_text,
      ticket_source = case when p_ticket_source = 'avg' then 'avg' else 'model' end,
      article_tickets = p_article_tickets, article_writer = p_article_writer,
      p1_lane = p_p1_lane, p1_prob = p_p1_prob, price = coalesce(p_price, 300), created_at = now()
    where id = ex.id;
    return 'updated';
  end if;
  perform pg_advisory_xact_lock(hashtext(p_publication_date::text));
  select coalesce(min(sequence_no), 0) - 1 into v_seq
  from public.note_publications where publication_date = p_publication_date and sequence_no < 0;
  insert into public.note_publications (
    race_key, publication_date, venue, race_no, sequence_no, publication_type, price, trigger_reason,
    p1_lane, p1_prob, title, article_text, status, ticket_source, article_tickets, article_writer
  ) values (
    p_race_key, p_publication_date, p_venue, p_race_no, v_seq, 'PAID', coalesce(p_price, 300),
    '自動下書き（締切前の通知）', p_p1_lane, p_p1_prob, p_title, p_article_text, 'AUTO_DRAFT',
    case when p_ticket_source = 'avg' then 'avg' else 'model' end, p_article_tickets, p_article_writer
  )
  on conflict (race_key) do nothing;
  if not found then
    return 'manual';
  end if;
  return 'created';
end;
$function$;

revoke all on function public.note_morning_save(text, date, text, integer, text, text, text, jsonb, text, integer, double precision, integer) from public, anon, authenticated;
revoke all on function public.note_morning_post_update(text, text, text, jsonb) from public, anon, authenticated;
revoke all on function public.note_morning_notify(integer, date) from public, anon, authenticated;
grant execute on function public.note_morning_save(text, date, text, integer, text, text, text, jsonb, text, integer, double precision, integer) to service_role;
grant execute on function public.note_morning_post_update(text, text, text, jsonb) to service_role;
grant execute on function public.note_morning_notify(integer, date) to service_role;
revoke all on function public.note_buyer_view(text, text) from public;
grant execute on function public.note_buyer_view(text, text) to anon, authenticated;
revoke all on function public.note_view_articles_v3(text, text, date, boolean) from public;
grant execute on function public.note_view_articles_v3(text, text, date, boolean) to anon, authenticated;

select cron.schedule(
  'note-morning',
  '2 0 * * *',
  $cmd$
select net.http_post(
  url := 'https://api.github.com/repos/tatsutatsu1028/-boatrace-ai/actions/workflows/note_morning.yml/dispatches',
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
  body := '{"ref":"main"}'::jsonb,
  timeout_milliseconds := 10000
) as request_id;
$cmd$
);

-- 締切前の通知（pg_cron notify-due-races-every-minute から1分おき）。20261107030000 に、朝の記事＋自動更新のときの分岐を足した
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
