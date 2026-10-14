-- 本番の予想モデルの切り替え（今のモデル → 新しいモデル ml-chain）。
--
--   app_settings.prediction_model             : 本番の予想モデル。'current'（今のモデル）/ 'ml'（新しいモデルの最新の版）
--                                               / 'ml-chain-...'（その版に固定）。'current' に戻せばいつでも元に戻る
--   app_settings.prediction_model_switched_at : 最後に prediction_model を変えた日時（トリガーで自動記録）
--   prediction_model_switches                 : 変えるたびに1行（いつ・何から・何へ）。戻したときも残る
--   prediction_results.model_version          : 検証結果の行の予想に使ったモデルの版（固定予想の model_version）。
--                                               検証画面で切り替え前（今のモデル）と後（新しいモデル）を分ける
--
-- 読むのはアプリ（Publishable key）と GitHub Actions（service_role）。prediction_model_switches は
-- トリガー（security definer）だけが書き、anon / authenticated には読み取りだけ許す。

alter table public.app_settings
  add column if not exists prediction_model text not null default 'current',
  add column if not exists prediction_model_switched_at timestamp with time zone;

alter table public.app_settings
  drop constraint if exists app_settings_prediction_model_check;
alter table public.app_settings
  add constraint app_settings_prediction_model_check
  check (prediction_model in ('current', 'ml') or prediction_model like 'ml-chain-%');

create table if not exists public.prediction_model_switches (
  id bigint generated always as identity primary key,
  switched_at timestamptz not null default now(),
  from_model text,
  to_model text not null
);

alter table public.prediction_model_switches enable row level security;

drop policy if exists prediction_model_switches_read on public.prediction_model_switches;
create policy prediction_model_switches_read
  on public.prediction_model_switches
  for select
  to anon, authenticated
  using (true);

create or replace function public.record_prediction_model_switch()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.prediction_model is distinct from old.prediction_model then
    new.prediction_model_switched_at := now();
    insert into public.prediction_model_switches (switched_at, from_model, to_model)
    values (now(), old.prediction_model, new.prediction_model);
  end if;
  return new;
end;
$$;

revoke all on function public.record_prediction_model_switch() from public, anon, authenticated;

drop trigger if exists app_settings_prediction_model_switch on public.app_settings;
-- 更新のときだけ（設定画面の保存は upsert で、挿入を試みる段階では prediction_model を含まないため）
create trigger app_settings_prediction_model_switch
  before update of prediction_model on public.app_settings
  for each row execute function public.record_prediction_model_switch();

alter table public.prediction_results
  add column if not exists model_version text;

-- 既存の検証結果に、固定予想に残っている版を入れる（すべて今のモデル）
update public.prediction_results r
set model_version = coalesce(
  s.payload_json::jsonb ->> 'model_version',
  s.payload_json::jsonb -> 'final' -> 0 ->> 'model_version'
)
from public.prediction_snapshots s
where s.race_key = r.race_key
  and r.model_version is null;
