"""チルトを1着モデルの学習特徴量(BASE_NUM)に追加した場合のバックテスト。

history_full.csv で、艇番×チルトの1着率に差がある（外枠はチルトを
上げるほど1着率が上がり、1号艇は下がる）ことを確認したため、
1着モデルの特徴量に tilt を足すとテスト期間の成績がどう変わるかを
日付順の学習/テスト分割で比較する。

比較する3パターン（2着・3着モデル・predict()の補正は全パターン共通）:
  P  : 現行本番と同じ。1着モデルは sample_history.csv で学習（チルト列なし）
  A  : 1着モデルを history_full.csv の学習期間で学習、BASE_NUM は現状のまま
  B  : A と同じ学習データで BASE_NUM に tilt を追加
  A と B の差が「チルト追加の純粋な効果」。

リーク対策:
  prediction.train() はデータフォルダ（data_paths.py）の history_full.csv を
  読んで2着・3着モデルを学習する。テスト期間を含む本物のCSVを読ませると
  リークになるため、学習期間の行だけを書いた history_full.csv を一時
  フォルダに置き、BOATRACE_DATA_DIR をそこへ向けてから train() を呼ぶ。
  train()/predict()/trifecta() の中身は一切変更しない。

評価指標（テスト期間）:
  - 1着的中率: predict() 後の p_first 最大艇が実際に1着か
    （参考として、補正前の1着モデル素の確率でも算出）
  - 実1着艇に与えた確率の平均、レース単位の対数損失
  - 3連単回収率: trifecta() の確率上位 N 点を各100円購入
    （history_full.csv に単勝払戻は保存していないため、1着の回収率は
      3連単で代用する。本命1点・上位5点・上位10点）
  - 本命艇の艇番別の選択数・的中率

使い方:
  python backtest_tilt_first_model.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from joblib import Parallel, delayed
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import data_paths  # noqa: E402
import prediction  # noqa: E402

ORIGINAL_BASE_NUM = list(prediction.BASE_NUM)
TILT_BASE_NUM = ORIGINAL_BASE_NUM + ["tilt"]

# (名前, 学習期間の最終日, テスト開始日)
SPLITS = [
    ("分割1: 学習6月 → テスト9/8〜9/25", "20260630", "20260901"),
    ("分割2: 学習6月+9/8〜9/16 → テスト9/17〜9/25", "20260916", "20260917"),
]

TOP_NS = (1, 5, 10)


def _load_history():
    df = pd.read_csv(
        data_paths.data_path("history_full.csv"),
        dtype={"race_date": str, "jcd": str, "racer_id": str},
    )
    df["lane"] = pd.to_numeric(df["lane"], errors="coerce")
    df["finish"] = pd.to_numeric(df["finish"], errors="coerce")
    df = df.dropna(subset=["lane", "finish", "race_key"])
    df["lane"] = df["lane"].astype(int)
    df["finish"] = df["finish"].astype(int)
    # 6艇そろい、1着が1艇だけのレースに限定する。
    g = df.groupby("race_key")
    ok = (g["lane"].transform("nunique") == 6) & (
        g["finish"].transform(lambda s: int((s == 1).sum())) == 1
    )
    return df[ok].copy()


def _train(first_history, position_history_path, base_num):
    """prediction.train() を、指定した履歴CSVを内部参照させて呼ぶ。"""
    prediction.BASE_NUM = list(base_num)
    original_dir = os.environ.get(data_paths.ENV_NAME)
    os.environ[data_paths.ENV_NAME] = str(position_history_path.parent)
    try:
        return prediction.train(first_history)
    finally:
        if original_dir is None:
            os.environ.pop(data_paths.ENV_NAME, None)
        else:
            os.environ[data_paths.ENV_NAME] = original_dir


def _evaluate_chunk(model, base_num, races):
    prediction.BASE_NUM = list(base_num)
    rows = []
    for race_key, race in races:
        race = race.sort_values("lane").reset_index(drop=True)
        winner = int(race.loc[race["finish"] == 1, "lane"].iloc[0])

        # 1着モデル素の確率（predict()の補正前）
        raw = model.predict_proba(
            race.reindex(columns=base_num + prediction.BASE_CAT)
        )[:, 1]
        raw = raw / raw.sum()

        final = prediction.predict(model, race)
        p = final.set_index("lane")["p_first"].astype(float)
        p = p / p.sum()

        tri = prediction.trifecta(final).sort_values("prob", ascending=False)
        actual_combo = str(race["trifecta"].iloc[0]).strip()
        payout = float(pd.to_numeric(
            race["trifecta_payout_per_100"].iloc[0], errors="coerce"
        ) or 0.0)
        combos = tri["combo"].tolist()

        rec = {
            "race_key": race_key,
            "winner": winner,
            "pick": int(p.idxmax()),
            "pick_raw": int(race["lane"].iloc[int(np.argmax(raw))]),
            "p_winner": float(p.get(winner, np.nan)),
            "p_winner_raw": float(raw[race["lane"].to_numpy() == winner][0]),
        }
        for n in TOP_NS:
            hit = actual_combo in combos[:n]
            rec[f"tri_hit_{n}"] = int(hit)
            rec[f"tri_pay_{n}"] = payout if hit else 0.0
        rows.append(rec)
    return rows


def _evaluate(model, base_num, test, n_jobs=-1):
    races = list(test.groupby("race_key", sort=False))
    chunks = [races[i::32] for i in range(32)]
    parts = Parallel(n_jobs=n_jobs)(
        delayed(_evaluate_chunk)(model, base_num, c) for c in chunks
    )
    return pd.DataFrame([r for part in parts for r in part])


def _summary(res):
    n = len(res)
    out = {
        "レース数": n,
        "1着的中率(predict後)": (res["pick"] == res["winner"]).mean(),
        "1着的中率(1着モデル素)": (res["pick_raw"] == res["winner"]).mean(),
        "実1着艇への平均確率": res["p_winner"].mean(),
        "対数損失(predict後)": -np.log(res["p_winner"].clip(1e-9)).mean(),
        "対数損失(1着モデル素)": -np.log(res["p_winner_raw"].clip(1e-9)).mean(),
    }
    for k in TOP_NS:
        out[f"3連単上位{k}点 的中率"] = res[f"tri_hit_{k}"].mean()
        out[f"3連単上位{k}点 回収率"] = res[f"tri_pay_{k}"].sum() / (100.0 * k * n)
    return out


def _lane_table(res):
    t = res.assign(hit=(res["pick"] == res["winner"]).astype(int))
    g = t.groupby("pick")["hit"].agg(["size", "mean"])
    g.columns = ["本命数", "的中率"]
    return g


def _bootstrap_diff(res_a, res_b, n_boot=2000, seed=0):
    """同一レース対応のブートストラップで B-A の差の95%区間を出す。"""
    m = res_a.merge(res_b, on="race_key", suffixes=("_a", "_b"))
    hit_a = (m["pick_a"] == m["winner_a"]).to_numpy(dtype=float)
    hit_b = (m["pick_b"] == m["winner_b"]).to_numpy(dtype=float)
    pay_a = m["tri_pay_5_a"].to_numpy()
    pay_b = m["tri_pay_5_b"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(m), size=(n_boot, len(m)))
    d_hit = hit_b[idx].mean(axis=1) - hit_a[idx].mean(axis=1)
    d_roi = (pay_b[idx].mean(axis=1) - pay_a[idx].mean(axis=1)) / 500.0
    return {
        "1着的中率差": (hit_b.mean() - hit_a.mean(), *np.percentile(d_hit, [2.5, 97.5])),
        "3連単上位5点回収率差": (
            (pay_b.mean() - pay_a.mean()) / 500.0,
            *np.percentile(d_roi, [2.5, 97.5]),
        ),
    }


def main():
    hist = _load_history()
    sample = pd.read_csv(data_paths.data_path("sample_history.csv"))

    for label, train_end, test_start in SPLITS:
        train_df = hist[hist["race_date"] <= train_end].copy()
        test_df = hist[hist["race_date"] >= test_start].copy()
        print("=" * 72)
        print(label)
        print(
            f"  学習 {train_df['race_key'].nunique()}R / "
            f"テスト {test_df['race_key'].nunique()}R"
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_hist = Path(tmp) / "history_full.csv"
            train_df.to_csv(tmp_hist, index=False)

            variants = {
                "P 現行(sample_history学習)": (sample, ORIGINAL_BASE_NUM),
                "A history_full学習・チルトなし": (train_df, ORIGINAL_BASE_NUM),
                "B history_full学習・チルトあり": (train_df, TILT_BASE_NUM),
            }
            results = {}
            for name, (first_hist, base_num) in variants.items():
                model = _train(first_hist, tmp_hist, base_num)
                results[name] = _evaluate(model, base_num, test_df)

        prediction.BASE_NUM = list(ORIGINAL_BASE_NUM)

        summary = pd.DataFrame({k: _summary(v) for k, v in results.items()})
        with pd.option_context("display.float_format", "{:.4f}".format,
                               "display.width", 200):
            print(summary.to_string())
            print("\n  [本命艇の艇番別 本命数・的中率]")
            lane = pd.concat(
                {k.split(" ")[0]: _lane_table(v) for k, v in results.items()},
                axis=1,
            )
            print(lane.to_string())

        diff = _bootstrap_diff(
            results["A history_full学習・チルトなし"],
            results["B history_full学習・チルトあり"],
        )
        print("\n  [B-A の差 (点推定, 95%ブートストラップ区間)]")
        for k, (pt, lo, hi) in diff.items():
            print(f"    {k}: {pt:+.4f}  [{lo:+.4f}, {hi:+.4f}]")


if __name__ == "__main__":
    main()
