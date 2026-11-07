-- 締切前の Discord 通知と同じタイミングで、展示後の買い目を使った note 有料記事の下書きを自動で作る（note_auto.py）。
-- 記事は PIN なしで開ける記事表示ページ（app.py の ?view=note、note_page.py）で読み、秘密のキーが合うときだけ返す。
--
--   note_publications.status = 'AUTO_DRAFT' : 自動下書き。今までの「確定済みの記事」（DRAFT 以降）とは別扱いで、
--       アプリの「確定済み」表示・本日N本目・夜のまとめ（的中判定）には数えない。sequence_no は負の番号（-1, -2, …）。
--       アプリで「note記事用に確定」を押すと、その行を手動の記事（DRAFT・正の番号）に置き換える（reserve_note_publication_v2）
--   app_settings.note_auto_enabled       : 自動下書きの ON/OFF（初期値 OFF。ON にすると通知が記事を待つ・URL を載せる）
--   app_settings.note_auto_ticket_source : 記事に載せる買い目の既定（model = モデルのみ予想 / avg = 両方の予想平均）
--   app_settings.note_view_base_url      : 記事表示ページの URL の頭（アプリの URL。例 https://xxxx.streamlit.app）
--   auto_post_log.note_status            : ok（Claude の本文）/ template（Claude 失敗・買い目だけの型）/ manual（手動の記事あり）/ failed
--   vault の note_view_key               : 記事表示ページの秘密のキー（ここで乱数から作る。値はコードに書かない）
--
-- notify_due_races: 自動下書きが ON のとき、記事ができる（note_status が入る）まで待って送る。待つのは展示後の予想の保存から
-- 最大3分（または今までどおり締切7分前まで）。通知に記事表示ページの URL（キー付き）か「記事生成失敗」を載せる。OFF なら今までと同じ。

alter table public.note_publications drop constraint if exists note_publications_status_check;
alter table public.note_publications add constraint note_publications_status_check
  check (status = any (array['DRAFT', 'PUBLISHED', 'SETTLED', 'AUTO_DRAFT']));

alter table public.app_settings
  add column if not exists note_auto_enabled boolean not null default false,
  add column if not exists note_auto_ticket_source text not null default 'model',
  add column if not exists note_view_base_url text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'app_settings_note_auto_ticket_source_check') then
    alter table public.app_settings add constraint app_settings_note_auto_ticket_source_check
      check (note_auto_ticket_source in ('model', 'avg'));
  end if;
end $$;

alter table public.auto_post_log
  add column if not exists note_status text,
  add column if not exists note_reason text,
  add column if not exists note_seconds real,
  add column if not exists note_at timestamptz;

comment on column public.auto_post_log.note_status is
  'note 自動下書き: ok / template（Claude 失敗・買い目だけの型）/ manual（手動の記事あり・作らない）/ failed';

-- 記事表示ページの秘密のキー（無ければ作る。値はリポジトリに残らない）
do $$
begin
  if not exists (select 1 from vault.secrets where name = 'note_view_key') then
    perform vault.create_secret(encode(extensions.gen_random_bytes(24), 'hex'), 'note_view_key',
                                'note 記事表示ページ（?view=note）のキー');
  end if;
end $$;

-- 自動下書きを保存する（GitHub Actions の service_role だけが呼ぶ）。
-- 戻り値: created / updated / manual（手動の記事が既にあるので書かない）
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
    if ex.status <> 'AUTO_DRAFT' then
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

revoke all on function public.note_auto_save_draft(
  text, date, text, integer, text, text, text, jsonb, text, integer, double precision, integer
) from public, anon, authenticated;
grant execute on function public.note_auto_save_draft(
  text, date, text, integer, text, text, text, jsonb, text, integer, double precision, integer
) to service_role;

-- 記事枠の確定（アプリの「note記事用に確定」）。自動下書きがあれば、それを手動の記事に置き換える
-- （本日N本目の番号を振り、status = DRAFT・中身は押したときのもの）。それ以外は 20261021010000 と同じ。
-- 自動下書きの行を書き換えるため security definer にする（anon には update の権限が無い）。
create or replace function public.reserve_note_publication_v2(
  p_race_key text, p_publication_date date, p_venue text, p_race_no integer, p_trigger_reason text,
  p_p1_lane integer, p_p1_prob double precision, p_title text, p_article_text text,
  p_ticket_source text, p_article_tickets jsonb, p_article_writer text
)
returns public.note_publications
language plpgsql
security definer
set search_path to ''
as $function$
declare
  existing public.note_publications;
  next_no integer;
  created public.note_publications;
begin
  select * into existing from public.note_publications where race_key = p_race_key;
  if found and existing.status <> 'AUTO_DRAFT' then
    return existing;
  end if;
  perform pg_advisory_xact_lock(hashtext(p_publication_date::text));
  select coalesce(max(sequence_no), 0) + 1 into next_no
  from public.note_publications where publication_date = p_publication_date and sequence_no > 0;
  if existing.id is not null then
    update public.note_publications set
      publication_date = p_publication_date, venue = p_venue, race_no = p_race_no, sequence_no = next_no,
      publication_type = 'PAID', price = 300, trigger_reason = p_trigger_reason,
      p1_lane = p_p1_lane, p1_prob = p_p1_prob, title = p_title, article_text = p_article_text,
      status = 'DRAFT',
      ticket_source = case when p_ticket_source in ('avg', 'aim') then p_ticket_source else 'model' end,
      article_tickets = p_article_tickets, article_writer = p_article_writer, created_at = now()
    where id = existing.id and status = 'AUTO_DRAFT'
    returning * into created;
    if created.id is not null then
      return created;
    end if;
    select * into existing from public.note_publications where race_key = p_race_key;
    return existing;
  end if;
  insert into public.note_publications (
    race_key, publication_date, venue, race_no, sequence_no,
    publication_type, price, trigger_reason, p1_lane, p1_prob,
    title, article_text, ticket_source, article_tickets, article_writer
  ) values (
    p_race_key, p_publication_date, p_venue, p_race_no, next_no,
    'PAID', 300, p_trigger_reason, p_p1_lane, p_p1_prob, p_title, p_article_text,
    case when p_ticket_source in ('avg', 'aim') then p_ticket_source else 'model' end,
    p_article_tickets, p_article_writer
  )
  returning * into created;
  return created;
end;
$function$;

grant execute on function public.reserve_note_publication_v2(
  text, date, text, integer, text, integer, double precision, text, text, text, jsonb, text
) to anon, authenticated;

-- 記事表示ページ（PIN なし）用。キーが vault の note_view_key と一致したときだけ行を返す（違えば0行）。
--   p_race_key あり: そのレースの記事（自動下書き・手動の記事どちらも）
--   p_race_key なし: その日（省略時は今日・日本時間）の自動下書きを締切順に
create or replace function public.note_view_articles(p_key text, p_race_key text default null, p_date date default null)
returns table (
  race_key text, publication_date date, venue text, race_no integer, title text, article_text text,
  price integer, status text, ticket_source text, article_writer text, created_at timestamptz, deadline text
)
language sql
stable
security definer
set search_path to 'public'
as $function$
  select n.race_key, n.publication_date, n.venue, n.race_no, n.title, n.article_text,
         n.price, n.status, n.ticket_source, n.article_writer, n.created_at,
         coalesce((select p.deadline from public.pre_exhibition_picks p where p.race_key = n.race_key limit 1),
                  (select m.deadline from public.manshu_chance_picks m where m.race_key = n.race_key limit 1)) as deadline
  from public.note_publications n
  where length(coalesce(p_key, '')) >= 16
    and exists (select 1 from vault.decrypted_secrets s
                where s.name = 'note_view_key' and s.decrypted_secret = p_key)
    and (case when nullif(p_race_key, '') is not null then n.race_key = p_race_key
              else n.status = 'AUTO_DRAFT'
                   and n.publication_date = coalesce(p_date, (now() at time zone 'Asia/Tokyo')::date) end)
  order by deadline nulls last, n.race_key;
$function$;

revoke all on function public.note_view_articles(text, text, date) from public;
grant execute on function public.note_view_articles(text, text, date) to anon, authenticated;

-- 記事表示ページの一覧が0行のとき、キー自体が正しいか（正しいときだけ「まだありません」と出す）
create or replace function public.note_view_key_ok(p_key text)
returns boolean
language sql
stable
security definer
set search_path to 'public'
as $function$
  select length(coalesce(p_key, '')) >= 16
     and exists (select 1 from vault.decrypted_secrets s where s.name = 'note_view_key' and s.decrypted_secret = p_key);
$function$;

revoke all on function public.note_view_key_ok(text) from public;
grant execute on function public.note_view_key_ok(text) to anon, authenticated;

-- 設定タブに出す記事表示ページの一覧の URL（キー付き）。アプリの service_key（サーバー側だけ）でしか呼べない
create or replace function public.note_view_link()
returns text
language sql
stable
security definer
set search_path to 'public'
as $function$
  select rtrim(s.note_view_base_url, '/') || '/?view=note&key=' || k.decrypted_secret
  from public.app_settings s, vault.decrypted_secrets k
  where s.id = 1 and nullif(trim(s.note_view_base_url), '') is not null and k.name = 'note_view_key'
  limit 1;
$function$;

revoke all on function public.note_view_link() from public, anon, authenticated;
grant execute on function public.note_view_link() to service_role;

-- 締切前の通知（pg_cron notify-due-races-every-minute から1分おき）。20261104000000 に、自動下書きの待ち合わせと
-- 記事表示ページの URL・「記事生成失敗」の行を足した。自動下書きが OFF なら今までと同じ
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
begin
  select decrypted_secret into v_url
  from vault.decrypted_secrets where name = 'discord_webhook_boatrace' limit 1;
  select decrypted_secret into v_url_manshu
  from vault.decrypted_secrets where name = 'discord_webhook_manshu' limit 1;
  v_url_manshu := coalesce(v_url_manshu, v_url);
  if v_url is null then return 0; end if;
  select coalesce(s.note_auto_enabled, false), nullif(trim(s.note_view_base_url), '')
    into v_note_on, v_base
  from public.app_settings s where s.id = 1;
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
    if v_note_on then
      v_link := case when v_base is not null and v_key is not null
                     then '<' || rtrim(v_base, '/') || '/?view=note&race_key=' || r.race_key || '&key=' || v_key || '>'
                     else '（記事ページのURLが未設定：設定タブで登録）' end;
      v_msg := v_msg || E'\n' || case r.note_status
        when 'ok' then '📝 note記事（自動下書き）: ' || v_link
        when 'manual' then '📝 note記事（手動で確定済み）: ' || v_link
        when 'template' then '⚠️ 記事生成失敗（Claudeの本文を作れず、買い目だけの型で保存: '
                             || left(coalesce(r.note_reason, ''), 80) || '）' || E'\n📝 ' || v_link
        when 'failed' then '⚠️ 記事生成失敗: ' || left(coalesce(r.note_reason, ''), 120)
        else '⚠️ 記事生成失敗（通知までに記事ができませんでした。後でできたらこのページに出ます）' || E'\n📝 ' || v_link
      end;
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
