# 「1号艇が負けた場合の着順」専用モデル（ml-upset。検証中）

万舟チャンスのレースで「1号艇が負けるなら誰が勝つか、2・3着は誰か」をよく当てるために作った専用モデル。
**本番の予想・買い目・画面には使っていない。** 今の新モデル（ml-chain-v2）のファイル・設定・毎週の学習
（`ml_nightly.py`）には手を入れていない。

検証の結果（成績の数字）は公式データから計算したものなので、非公開のデータ用リポジトリの
`reports/upset_v1/REPORT.md` にだけ置いている。

## 仕組み（`ml_upset.py`）

- 今の新モデルと同じ3段（`ml_model.ChainModel` を引き継ぐ `UpsetModel`）・同じ特徴量・同じ目的関数
- 学習は2年分のうち1号艇が出走して1着でなかったレースだけ
  - 1着: 2〜6号艇の中の割合（1号艇の行は使わない）
  - 2着・3着: 1号艇も候補に入れる（1着が1号艇以外のときだけ）
  - 艇番を除いた実力のモデルは全レースで学習（艇番に関係ない強さなので絞らない）
- 展示前の版は展示前モデル（`ml_pre`）と同じ列
- 3連単（`ml_upset.combine`）: 1号艇1着の組は今のモデルのまま、1号艇以外が1着の組だけ
  「（1 − 今のモデルの1号艇の1着確率）× 専用モデルの P(a) × P(b|a) × P(c|a,b)」に置き換える。合計は1のまま
- 学習・テストは時期で分ける（今の新モデルの検証と同じ期間。`tools/ml_upset_experiment.py`）

## 学習済みファイル（データ用リポジトリ。本番とは別の名前）

| 版 | ファイル | 最新の版 |
|---|---|---|
| 展示後 | `models/ml-upset-v1-lgb-2y-<基準日>.joblib` | `models/upset_latest.json` |
| 展示前 | `models/ml-upset-pre-v1-lgb-2y-<基準日>.joblib` | `models/upset_pre_latest.json` |

`ML Upset Model` ワークフロー（`ml_upset.yml`。手動）で学習し、この2組のファイルだけをコミットする。
本番の `models/latest.json`・`pre_latest.json`・`ml-chain-*`・`ml_state.pkl.gz` には触れない。

## 毎晩の検証（`upset_compare.py`）

`hindcast.yml` の `manshu_compare.py` の後に動く。万舟チャンスの印のレース（10/17〜）について

- 展示後版: 事後予想の3連単確率（`hindcast_predictions.tri_probs`）を専用モデルで置き換えたもの
- 朝版: 朝の3連単確率（`manshu_chance_picks.tri_probs`）を専用モデルの展示前の版で置き換えたもの

を `manshu_upset_compare` に保存する（荒れたときの本命・2番手・1号艇が負ける確率も）。専用モデルの版は、
その日より前のデータだけで学習・調整した版のうちいちばん新しいもの。

あわせて確定3連単オッズ（`manshu_aim_compare.final_odds`）で、今のモデルと専用モデルの両方の確率から
作り方A（1号艇1着を外す・中穴5点＋万舟5点・確率順）と E（1着を上位2艇に固定・確率順10点）を作り、
同じレース・同じオッズで並べる。検証タブ（万舟チャンス）の一番下に表で出す（`upset_view.py`）。

使うには:

1. `supabase/migrations/20261027000000_manshu_upset_compare.sql` を適用する
2. `ML Upset Model` を `as_of=20261016` で動かす（10/17 からさかのぼる分の版）。以後の版は必要なときに動かす

## 画面（合格してから。まだ入れていない）

万舟チャンスの各レースに「荒れたときの本命：○号艇（○%）」「2番手：○号艇」を出す予定。
値は `ml_upset.upset_summary`（`manshu_upset_compare.post_summary`／`pre_summary`）にある。
