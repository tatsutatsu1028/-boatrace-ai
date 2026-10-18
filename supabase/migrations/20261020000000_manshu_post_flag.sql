-- 「💥 万舟チャンス」の一覧を「🌅 展示前厳選10」と同じ扱いにする: 展示後に予想したときの本命・3連単の確率・印。
--
--   post_favorite_lane : 展示後の予想の本命（1着確率が最も高い艇）
--   post_tri_probs     : 展示後の予想の3連単の全120通りの確率（一覧で中穴〜万舟狙いの買い目を作り直すのに使う）
--   post_flag          : 展示後に評価が大きく変わった（本命が変わった、または万舟確率が10ポイント以上下がった）
--   post_flag_reasons  : その理由
--
-- 展示前厳選10（pre_pick_apply_post）と同じ決め方。印が付いても一覧からは外さない。

alter table public.manshu_chance_picks
  add column if not exists post_favorite_lane smallint,
  add column if not exists post_tri_probs jsonb,
  add column if not exists post_flag boolean,
  add column if not exists post_flag_reasons text[];

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
    post_flag = cardinality(reasons) > 0,
    post_flag_reasons = reasons,
    post_saved_at = s.saved_at,
    post_pre_exhibition = s.pre_exhibition
  where race_key = p_race_key;
end;
$$;

revoke all on function public.manshu_pick_apply_post(text) from public, anon, authenticated;
