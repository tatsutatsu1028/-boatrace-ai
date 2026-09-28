# 過去データ（モデル作り直し用）の中身と集め方

予想・学習にはまだ使っていない。集めているだけ。

## 保存場所

`data/history/<種類>/<年>/<種類>_<年>-<月>.csv`（集め終わった月は `.csv.gz`）

| 種類 | 1行の単位 | 取得元 | 読み込み |
|---|---|---|---|
| `k_results` | 1レース1艇 | 公式ダウンロード「競走成績」(Kファイル) | `history_store.read_kind("k_results", start, end)` |
| `k_payouts` | 1レース1券種1組番 | 同上 | `read_kind("k_payouts")` |
| `b_programs` | 1レース1艇 | 公式ダウンロード「番組表」(Bファイル) | `read_kind("b_programs")` |
| `pages` | 1レース1艇 | 出走表ページ＋直前情報ページ | `read_kind("pages")` |
| `official_dates.csv` | 1日1ファイル | K/Bの取得記録（ok / no_file / error） | |
| `pages_failed.csv` | 1レース | ページ取得に失敗したレース（3回失敗で打ち切り） | |

全種類とも `race_key`（`YYYYMMDD_場コード_R`）と `lane`（艇番）で結合できる。
月ごとに分けているので、1ファイルが100MBを超えることはない（1か月 約5〜7MB、圧縮後 約1〜2MB）。
Supabaseには保存しない。

## 公式ダウンロードファイルに含まれる項目

### 競走成績（`k_results`、Kファイル）

| 列 | 内容 | 備考 |
|---|---|---|
| `race_date` `jcd` `venue` `race_no` `race_key` | 日付・場・R | |
| `meet_title` | 節の名前（例: スポーツニッポン杯） | 2026-09-28 より前に取得した分は「第4回…」のように「第」を含む節名が空欄（約31%）。正式な節名は `pages` の `meet_title` を使う |
| `day_no` | 節の何日目（第3日 → 3） | |
| `race_type` | レース名（予選・一般戦・準優勝戦・優勝戦・選抜戦・予選特賞 など） | 場ごとの独自名（「ランチタイム」等）もそのまま |
| `fixed_entry` | 進入固定レースなら1 | |
| `distance_m` | 距離（1800 / 1200 など） | |
| `weather` | 天候（晴・曇り・雨・雪・霧） | |
| `wind_direction` | 風向（北・北東…・無風） | 文字で入っている |
| `wind_speed` | 風速 (m) | |
| `wave_cm` | 波高 (cm) | |
| `kimarite` | 決まり手（逃げ・差し・まくり・まくり差し・抜き・恵まれ） | |
| `lane` `racer_id` `racer_name` | 艇番・登録番号・氏名 | |
| `finish` | **着順 1〜6**（フライング・失格・欠場などは空欄） | |
| `finish_raw` | 着順欄の元の表記（01〜06、F、L0/L1、K0/K1、S0/S1/S2 など） | 事故の種類が分かる |
| `motor_no` `boat_no` | モーター番号・ボート番号 | |
| `exhibition_time` | 展示タイム | |
| `course` | **実際の進入コース** | |
| `st` / `st_raw` | スタートタイミング（フライングは負の値） | |
| `race_time_sec` / `race_time_raw` | レースタイム（秒）。4着以下は空欄のことが多い | |

### 払戻（`k_payouts`、Kファイル）

単勝・複勝・2連単・2連複・拡連複・3連単・3連複 の全組番の払戻金と人気。
不成立・特払いは `combo` にその文字、`payout` は空欄。

### 番組表（`b_programs`、Bファイル）

| 列 | 内容 |
|---|---|
| `meet_title` `day_no` `race_type` `fixed_entry` `distance_m` | 節名・何日目・レース名・進入固定・距離 |
| `deadline` | 締切予定時刻 |
| `racer_id` `racer_name` `age` `branch` `weight` `racer_class` | 登番・氏名・年齢・支部・体重・級別 |
| `national_win_rate` `national_2ren` | 全国勝率・2連率 |
| `local_win_rate` `local_2ren` | 当地勝率・2連率 |
| `motor_no` `motor_2ren` `boat_no` `boat_2ren` | モーター/ボートの番号・2連率 |
| `meet_results_raw` | 今節成績（前日までの着順を1文字ずつ。12文字＝6日×2走） |
| `hayami` | 早見（同じ日のもう1走のR番号） |

番組表は開催前に作られるので、悪天候で中止になった日の場も含まれる（その日は競走成績には無い）。

### K/Bファイルに無いもの

チルト・部品交換・プロペラ交換・展示ST・展示の進入コース・F数/L数・3連率（全国/当地/モーター/ボート）・グレード・気温・水温 → ページから取る（下）。
潮位はどこにも無い（対象外）。

## ページから取る項目（`pages`）

1レースにつき出走表と直前情報の2ページ。

| 列 | 内容 | 取得元 |
|---|---|---|
| `grade` / `grade_class` | グレード（SG・G1・G2・G3・一般）と元のclass名（`is-G1b` 等） | 出走表の見出し |
| `grade_extra` | 女子戦・ルーキー等の付加class | 〃 |
| `meet_title` `meet_days` `day_no` `day_label` `is_final_day` | 節名・節の日数・何日目・「初日/2日目/最終日」・最終日か | 日付タブ |
| `race_name` `race_category` | レース名と分類（予選 / 準優勝戦 / 優勝戦 / 特別選抜 / 一般 / その他） | 〃 |
| `distance_m` `fixed_entry` `stabilizer` `deadline` | 距離・進入固定・安定板使用・締切時刻 | 〃 |
| `racer_class` `branch` `birthplace` `age` `weight_racelist` | 級別・支部・出身地・年齢・体重 | 出走表 |
| `f_count` `l_count` `avg_st` | F数・L数・平均ST | 出走表 |
| `national_win_rate/2ren/3ren` `local_win_rate/2ren/3ren` | 全国・当地の勝率・2連率・3連率 | 出走表 |
| `motor_no` `motor_2ren` `motor_3ren` `boat_no` `boat_2ren` `boat_3ren` | モーター・ボートの番号・2連率・3連率 | 出走表 |
| `weight` `adjust_weight` | 当日体重・調整重量 | 直前情報 |
| `exhibition_time` `tilt` | 展示タイム・チルト | 直前情報 |
| `propeller` `propeller_new` | プロペラ欄（「新」なら交換） | 直前情報 |
| `parts_exchange` `parts_exchanged` `parts_exchange_count` | 部品交換（「ギヤ、キャブ」のように複数は「、」区切り） | 直前情報 |
| `exhibition_course` `exhibition_st` | スタート展示の進入コース・ST（フライングは負の値） | 直前情報 |
| `temperature` `water_temperature` `wind_speed` `wave_cm` | 気温（マイナスも可）・水温・風速・波高 | 直前情報（水面気象情報） |
| `weather` `weather_code` | 天候 | 〃 |
| `wind_direction_code` | 風向アイコンの番号（`is-windN` の N） | 〃 |
| `stadium_direction_code` | 水面の向きのアイコン番号（`is-directionN`） | 〃 |
| `weather_as_of_race` | 水面気象情報が何R時点のものか | 〃 |
| `racer_win_rate` `local_win_rate` `motor_2ren` `boat_2ren` | 既存の history_full.csv と同じ列 | 出走表 |

気象は直前情報に出ている「そのレースの直前（前のレース時点）」の値で、ライブ予想時に見られる値と同じ。
レース中の実際の天候・風向・風速・波高は `k_results` にある。

## 集め方

- ワークフロー: `.github/workflows/history_backfill.yml`
- 起動: Supabaseのpg_cronから毎晩2回（`supabase/migrations/20260928000000_trigger_history_backfill_via_pg_cron.sql`）
  - 22:40 JST 起動 → 03:50 に停止（K/Bの未取得分 → ページ）
  - 04:40 JST 起動 → 08:00 に停止（ページのみ）
  - 公式サイトのメンテナンス（4:00〜4:30）は自動で待機
- 範囲: 2024-09-27〜昨日。**新しい日付から順に**取る（まず直近1年、その後残り1年）
- 取得間隔: 全体でリクエスト開始間隔3.4秒以上（毎秒0.3リクエスト以下、1.5秒以上）。実測 0.26 req/s
- 既存の日次収集（Collect History）が動いている間は5分ずつ待つ
- 1時間ごとに途中保存してpush。止まっても次回は取得済みを飛ばして続きから
- ページ取得に3回失敗したレースは `pages_failed.csv` に残して以後は飛ばす

## history_full.csv との関係

- `finish` は既存の学習・コース基準値（`course_baseline.py`）と互換にするため、従来どおり「1〜3着以外は4」。
- 実際の1〜6着は新しい列 `finish_full` に入れる（日次収集は今後自動で入れる。既存行は `build_history_full.py --fill-finish-full` で競走成績から後付け）。
- 空白期間（2026-06-25〜09-07）は `build_history_full.py --start 20260625 --end 20260907` で `pages` と `k_results` から作る。
  同じ作り方で 2026-09-20〜26 の18レースを作り直し、既存行と25列すべて一致することを確認済み。
