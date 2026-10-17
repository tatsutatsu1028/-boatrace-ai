-- 「💥 万舟チャンス」の万舟狙いの買い目（検証中・参考）と、その日のうちの結果の記録。
--
--   tri_probs     : 朝の展示前モデルの3連単の全120通りの確率 {組番: 確率}（一覧で、その時点のオッズから買い目を作る）
--   aim_*         : 画面に表示した万舟狙いの買い目（締切前に最後に表示したもの）と結果
--                   買い目 [{combo, prob, odds, stake, payout}]。source は 'list'（一覧）か 'predict'（予想タブ）
--
-- アプリ（管理者の画面）は表を直接書かず、下の関数 manshu_save_aim を通して書く（締切前・印のレースだけ）。
-- 結果（aim_hit・aim_stake・aim_return）は、その日のうちに list_results.py、取りこぼしは翌朝の pre_picks.py が書く。

alter table public.manshu_chance_picks
  add column if not exists tri_probs jsonb,
  add column if not exists aim_tickets jsonb,
  add column if not exists aim_method text,
  add column if not exists aim_source text,
  add column if not exists aim_odds_at timestamptz,
  add column if not exists aim_saved_at timestamptz,
  add column if not exists aim_hit boolean,
  add column if not exists aim_stake integer,
  add column if not exists aim_return integer;

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
  select race_date, deadline into pk from public.manshu_chance_picks where race_key = p_race_key;
  if not found then
    return false;
  end if;
  if jsonb_typeof(p_tickets) <> 'array' or jsonb_array_length(p_tickets) > 20 then
    raise exception 'tickets must be an array of at most 20';
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
    aim_saved_at = now()
  where race_key = p_race_key and result_at is null;
  return found;
end;
$$;

revoke all on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) from public;
grant execute on function public.manshu_save_aim(text, jsonb, text, text, timestamptz) to anon, authenticated, service_role;
