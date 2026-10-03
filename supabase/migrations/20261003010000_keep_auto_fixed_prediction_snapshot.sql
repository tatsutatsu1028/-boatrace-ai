-- 自動固定（auto_random_fix.py）で保存済みのレースは、
-- 「AI最終予想」を締切前に押し直しても上書きしないようにする。
-- 戻り値の status は 'auto_fixed'。それ以外の判定は 20261003000000 と同じ。

create or replace function public.save_latest_prediction_snapshot(
  p_race_key text,
  p_venue text,
  p_collector_name text,
  p_saved_at text,
  p_payload_json text,
  p_payload_hash text,
  p_pre_exhibition boolean
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $function$
declare
  v_date_text text;
  v_jcd text;
  v_rno integer;
  v_race_date date;
  v_now_jst timestamp := (now() at time zone 'Asia/Tokyo');
  v_deadline text;
  v_closed boolean := false;
  v_kind text;
  existing record;
begin
  if p_race_key is null or p_race_key !~ '^[0-9]{8}_[0-9]{2}_[0-9]{1,2}$' then
    raise exception 'invalid race_key: %', p_race_key;
  end if;
  if coalesce(p_payload_json, '') = '' then
    raise exception 'payload_json is empty';
  end if;

  v_date_text := split_part(p_race_key, '_', 1);
  v_jcd := split_part(p_race_key, '_', 2);
  v_rno := split_part(p_race_key, '_', 3)::integer;
  v_race_date := to_date(v_date_text, 'YYYYMMDD');

  -- 過去日はバックテスト扱い。保存は初回だけで、上書きはしない。
  if v_race_date < v_now_jst::date then
    v_kind := 'backtest';
    v_closed := true;
  else
    v_kind := 'same_day';
    if v_race_date = v_now_jst::date then
      select s.deadlines ->> v_rno::text
        into v_deadline
        from public.daily_schedule s
       where s.race_date = v_date_text
         and s.jcd = v_jcd
       limit 1;
      if v_deadline ~ '^[0-9]{1,2}:[0-9]{2}$'
         and v_now_jst >= (v_race_date + v_deadline::time) then
        v_closed := true;
      end if;
    end if;
  end if;

  -- 結果保存済みのレースは、締切時刻が取れなくても上書きしない。
  if exists (
    select 1 from public.prediction_results r where r.race_key = p_race_key
  ) then
    v_closed := true;
  end if;

  -- 同じレースの同時保存を直列化する。
  perform pg_advisory_xact_lock(hashtext('prediction_snapshot:' || p_race_key));

  select p.saved_at, p.snapshot_kind, p.collector_name,
         p.pre_exhibition, p.payload_hash, p.update_count
    into existing
    from public.prediction_snapshots p
   where p.race_key = p_race_key;

  if not found then
    if v_closed and v_kind = 'same_day' then
      return jsonb_build_object('status', 'closed', 'exists', false);
    end if;

    insert into public.prediction_snapshots (
      race_key, collector_name, saved_at, race_date, venue, race_no,
      snapshot_kind, payload_json, pre_exhibition, payload_hash, update_count
    ) values (
      p_race_key,
      coalesce(nullif(p_collector_name, ''), 'owner'),
      p_saved_at,
      to_char(v_race_date, 'YYYY-MM-DD'),
      p_venue,
      v_rno,
      v_kind,
      p_payload_json,
      coalesce(p_pre_exhibition, false),
      p_payload_hash,
      0
    );

    return jsonb_build_object(
      'status', 'inserted',
      'exists', true,
      'saved_at', p_saved_at,
      'snapshot_kind', v_kind,
      'collector_name', coalesce(nullif(p_collector_name, ''), 'owner'),
      'pre_exhibition', coalesce(p_pre_exhibition, false),
      'payload_hash', p_payload_hash,
      'update_count', 0
    );
  end if;

  -- 自動固定（auto_random）で保存済みのレースは、締切前でも上書きしない。
  if existing.snapshot_kind = 'auto_random' then
    return jsonb_build_object(
      'status', 'auto_fixed',
      'exists', true,
      'saved_at', existing.saved_at,
      'snapshot_kind', existing.snapshot_kind,
      'collector_name', existing.collector_name,
      'pre_exhibition', existing.pre_exhibition,
      'payload_hash', existing.payload_hash,
      'update_count', existing.update_count
    );
  end if;

  if v_closed then
    return jsonb_build_object(
      'status', 'closed',
      'exists', true,
      'saved_at', existing.saved_at,
      'snapshot_kind', existing.snapshot_kind,
      'collector_name', existing.collector_name,
      'pre_exhibition', existing.pre_exhibition,
      'payload_hash', existing.payload_hash,
      'update_count', existing.update_count
    );
  end if;

  if existing.payload_hash is not distinct from p_payload_hash
     and existing.pre_exhibition = coalesce(p_pre_exhibition, false) then
    return jsonb_build_object(
      'status', 'unchanged',
      'exists', true,
      'saved_at', existing.saved_at,
      'snapshot_kind', existing.snapshot_kind,
      'collector_name', existing.collector_name,
      'pre_exhibition', existing.pre_exhibition,
      'payload_hash', existing.payload_hash,
      'update_count', existing.update_count
    );
  end if;

  update public.prediction_snapshots p
     set payload_json = p_payload_json,
         payload_hash = p_payload_hash,
         pre_exhibition = coalesce(p_pre_exhibition, false),
         saved_at = p_saved_at,
         collector_name = coalesce(nullif(p_collector_name, ''), 'owner'),
         snapshot_kind = v_kind,
         update_count = p.update_count + 1
   where p.race_key = p_race_key;

  return jsonb_build_object(
    'status', 'updated',
    'exists', true,
    'saved_at', p_saved_at,
    'snapshot_kind', v_kind,
    'collector_name', coalesce(nullif(p_collector_name, ''), 'owner'),
    'pre_exhibition', coalesce(p_pre_exhibition, false),
    'payload_hash', p_payload_hash,
    'update_count', existing.update_count + 1
  );
end;
$function$;

revoke all on function public.save_latest_prediction_snapshot(
  text, text, text, text, text, text, boolean
) from public;
grant execute on function public.save_latest_prediction_snapshot(
  text, text, text, text, text, text, boolean
) to anon, authenticated, service_role;
