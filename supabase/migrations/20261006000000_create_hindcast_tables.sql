-- 全レースの事後予想（hindcast.py）の保存先。
--
--   hindcast_predictions : 1レース1行。1着確率・本命・買い目候補・実際の3連単・的中・モデルの版
--   hindcast_inputs      : 1艇1行。予想に使った入力（その時点で分かっていた値だけ。結果の列は置かない）
--   hindcast_runs        : 1日1行。予想の設定・学習データの範囲。その日の保存が終わった印も兼ねる
--
-- 書き込みは GitHub Actions（service_role）だけ。アプリからは使わないので、
-- RLS を有効にしてポリシーは付けない（anon / authenticated からは読み書きできない）。

create table if not exists public.hindcast_predictions (
  race_key text not null,
  model_version text not null,
  race_date date not null,
  jcd text not null,
  venue text,
  race_no smallint not null,
  -- 1〜6号艇の1着確率（配列の1番目が1号艇）
  p_first real[] not null,
  favorite_lane smallint not null,
  favorite_prob real,
  confidence text,
  -- 買い目の候補 [{combo, group, prob}, ...]（本番の自動固定と同じ選び方・同じ順番）
  candidate_tickets jsonb not null,
  candidate_count smallint,
  candidate_hit_probability real,
  favorite_risk_score smallint,
  exhibition_count smallint,
  -- ここから結果（未確定・返還などは null）
  trifecta_actual text,
  trifecta_payout integer,
  winner_lane smallint,
  favorite_hit boolean,
  candidate_hit boolean,
  candidate_hit_rank smallint,
  created_at timestamptz not null default now(),
  primary key (race_key, model_version)
);

create index if not exists hindcast_predictions_race_date_idx
  on public.hindcast_predictions (race_date);

create table if not exists public.hindcast_inputs (
  race_key text not null,
  lane smallint not null,
  race_date date not null,
  input_version text not null,
  -- 出走表
  racer_id text,
  racer_name text,
  racer_class text,
  racer_win_rate real,
  local_win_rate real,
  motor_2ren real,
  boat_2ren real,
  avg_st real,
  f_count smallint,
  l_count smallint,
  -- 直前情報（気象は前のレース時点の値）
  weight real,
  exhibition_time real,
  exhibition_st real,
  tilt real,
  parts_exchanged smallint,
  temperature real,
  wind_speed real,
  water_temperature real,
  wave_height real,
  -- 今節成績（同じ節で、このレースより前に終わった走りだけ）
  current_meet_races smallint,
  current_meet_avg_finish real,
  current_meet_avg_finish_adjusted real,
  current_meet_top2_rate real,
  current_meet_avg_st real,
  -- 選手のコース別成績（級別審査期間）
  course_top3_rate real,
  course_avg_st real,
  course_start_rank real,
  -- 場のコース別入着率・決まり手（前月までの3か月）
  venue_course_1st real,
  venue_course_2nd real,
  venue_course_3rd real,
  venue_course_4th real,
  venue_course_5th real,
  venue_course_6th real,
  venue_kimarite_nige real,
  venue_kimarite_makuri real,
  venue_kimarite_sashi real,
  venue_kimarite_makuri_sashi real,
  venue_kimarite_nuki real,
  venue_kimarite_megumare real,
  created_at timestamptz not null default now(),
  primary key (race_key, lane)
);

create index if not exists hindcast_inputs_race_date_idx
  on public.hindcast_inputs (race_date);

create table if not exists public.hindcast_runs (
  race_date date not null,
  model_version text not null,
  input_version text not null,
  race_count integer not null,
  settings jsonb,
  train_info jsonb,
  code_sha text,
  created_at timestamptz not null default now(),
  primary key (race_date, model_version)
);

alter table public.hindcast_predictions enable row level security;
alter table public.hindcast_inputs enable row level security;
alter table public.hindcast_runs enable row level security;
