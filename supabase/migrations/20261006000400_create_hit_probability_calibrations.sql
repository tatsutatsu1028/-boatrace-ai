-- 「この買い目の的中確率」の表示補正（hit_calibration.py）。
--
--   hit_probability_calibrations : 補正1回1行。事後予想（hindcast_predictions）の
--                                  「表示値 → 候補内的中」から作った等張回帰の変換表と、
--                                  作った時点の当てはまり・時系列の検証
--
-- 補正前と補正後の両方を残し、補正後の値が実際の的中率と合っているかを後で確かめる:
--   hindcast_predictions.candidate_hit_probability_calibrated / hit_calibration_id
--     … 事後予想を保存した時点の補正（その日より前のデータで作ったもの）で変換した値
--   prediction_results.hit_probability_calibrated / hit_calibration_id
--     … 固定時に画面へ表示した補正後の値（hit_probability は従来どおり補正前）
--
-- 書き込みは GitHub Actions（service_role）だけ。変換表は秘密ではなく、アプリが
-- Publishable key で読むため、anon / authenticated に読み取りだけ許す。

create table if not exists public.hit_probability_calibrations (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  model_version text not null,
  method text not null default 'isotonic',
  sample_count integer not null,
  data_start date,
  data_end date,
  -- 学習データ全体の実際の的中率と、補正前の表示の平均
  hit_rate real,
  raw_mean real,
  -- 変換表 {"x": [補正前...], "y": [補正後...], "n": [各段の件数...]}（間は直線でつなぐ）
  knots jsonb not null,
  -- 補正前の帯ごとの件数・補正前・補正後・実際（学習データ）
  bands jsonb,
  -- 日付の古い側で作った補正を、新しい側で確かめた結果
  holdout jsonb,
  -- 事後予想に保存済みの補正後の値（保存時点の補正）と実際の的中の比較
  previous_check jsonb,
  code_sha text
);

create index if not exists hit_probability_calibrations_version_idx
  on public.hit_probability_calibrations (model_version, created_at desc);

alter table public.hit_probability_calibrations enable row level security;

drop policy if exists hit_probability_calibrations_read on public.hit_probability_calibrations;
create policy hit_probability_calibrations_read
  on public.hit_probability_calibrations
  for select
  to anon, authenticated
  using (true);

alter table public.hindcast_predictions
  add column if not exists candidate_hit_probability_calibrated real,
  add column if not exists hit_calibration_id bigint;

alter table public.prediction_results
  add column if not exists hit_probability_calibrated double precision,
  add column if not exists hit_calibration_id bigint;
