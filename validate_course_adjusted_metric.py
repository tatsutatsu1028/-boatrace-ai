"""current_meet_avg_finish_adjusted（コース補正あり今節平均着順）が、
従来の current_meet_avg_finish（単純平均）より実際の1着結果を
よく説明できるかを history_full.csv だけで簡易検証するスクリプト。

history_full.csv には今節成績の列が無い（当日ライブ取得の値のため）
ので、公式レース結果の履歴から「今節の過去走」を自前で再現する。

手順:
  1. 開催（節）を会場(jcd)ごとに、レース日の間隔が2日以上空いた
     ところで区切って復元する。
  2. コース基準（course_baseline.py と同じ考え方）を、直近の
     テスト期間より前のデータだけから算出する（リークを避ける）。
  3. 各選手の節内の出走を時系列に並べ、「その走の一つ前まで」の
     実着順の単純平均・コース補正後の平均を再現する
     （current_meet_fetcher.py の集計方法と同じ）。
  4. 直近のテスト期間（本データでは2026年9月8日〜21日のブロック）
     を検証対象とし、
       (a) 各指標単体でのAUC（値が低いほど1着になりやすい、として
           符号を反転してスコア化）
       (b) 学習CSVと同じ基礎特徴量 + 今節指標(旧or新)一本を足した
           HistGradientBoostingClassifierでのテストAUCと、
           今節指標列そのもののPermutation Importance
     を比較する。

注意（重要な制約）:
  history_full.csv の finish 列は 1〜3位のみ実際の着順で、4位以下は
  すべて 4 として記録されている（collect_history.py 参照）。
  そのため本検証で再現する「今節平均着順」は、ライブ取得の真の
  1〜6着とは尺度が異なる疑似指標であり、絶対値の解釈には注意が要る。
  ただし新旧どちらの指標も同じ疑似着順から作るため、両者の
  相対比較（どちらが1着をよく説明するか）としては公平に成立する。

使い方:
  python validate_course_adjusted_metric.py
"""

from __future__ import annotations

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

HIST_PATH = "history_full.csv"

# 直近のテスト期間（この日付以降を検証対象、これより前を基準値算出・学習に使う）。
TEST_START = "20260900"

BASE_NUM = [
    "race_no", "lane", "racer_win_rate", "local_win_rate",
    "motor_2ren", "boat_2ren", "avg_st",
]
BASE_CAT = ["venue"]


def _load():
    df = pd.read_csv(
        HIST_PATH,
        dtype={"race_date": str, "jcd": str},
    )
    df["lane"] = pd.to_numeric(df["lane"], errors="coerce")
    df["finish"] = pd.to_numeric(df["finish"], errors="coerce")
    df = df.dropna(subset=["lane", "finish", "race_date", "jcd", "racer_id"])
    df["lane"] = df["lane"].astype(int)
    df["finish"] = df["finish"].astype(int)
    return df


def _assign_meet_id(df):
    """会場ごとにレース日を並べ、間隔2日以上で節を区切る。"""
    df = df.copy()
    df["_date"] = pd.to_datetime(df["race_date"], format="%Y%m%d")

    meet_ids = pd.Series(index=df.index, dtype="int64")
    counter = 0
    for jcd, g in df.groupby("jcd"):
        dates = sorted(g["_date"].unique())
        date_to_meet = {}
        for d in dates:
            if not date_to_meet:
                counter += 1
            else:
                prev = max(dd for dd in date_to_meet if dd < d)
                if (d - prev).days > 1:
                    counter += 1
            date_to_meet[d] = counter
        idx = g.index
        meet_ids.loc[idx] = df.loc[idx, "_date"].map(date_to_meet)

    df["meet_id"] = meet_ids.astype(int)
    return df


def _course_baseline_from(df):
    g = df.groupby("lane")["finish"].mean()
    return {int(k): float(v) for k, v in g.items()}


def _reconstruct_meet_features(df, baseline):
    df = df.sort_values(["jcd", "meet_id", "racer_id", "_date", "race_no"]).copy()
    df["diff"] = df["finish"] - df["lane"].map(baseline)

    grp = df.groupby(["jcd", "meet_id", "racer_id"], sort=False)

    df["current_meet_avg_finish_reconstructed"] = (
        grp["finish"].transform(lambda s: s.shift(1).expanding().mean())
    )
    df["current_meet_avg_finish_adjusted_reconstructed"] = (
        grp["diff"].transform(lambda s: s.shift(1).expanding().mean())
    )
    df["current_meet_races_reconstructed"] = grp.cumcount()
    return df


def _pipeline():
    num_cols = BASE_NUM + ["meet_feature"]
    pre = ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]), num_cols),
        ("cat", Pipeline([
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("ohe", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]), BASE_CAT),
    ])
    return Pipeline([
        ("pre", pre),
        ("clf", HistGradientBoostingClassifier(random_state=0)),
    ]), num_cols


def main():
    df = _load()
    df = _assign_meet_id(df)

    baseline = _course_baseline_from(df[df["race_date"] < TEST_START])
    print("[コース基準(学習期間のみで算出)]")
    for lane in sorted(baseline):
        print(f"  {lane}号艇: {baseline[lane]:.3f}")

    df = _reconstruct_meet_features(df, baseline)

    train_mask = df["race_date"] < TEST_START
    test_mask = ~train_mask
    ready = df["current_meet_races_reconstructed"] >= 1
    test = df[test_mask & ready].copy()
    train = df[train_mask].copy()

    y_test = (test["finish"] == 1).astype(int).to_numpy()
    print(f"\n検証件数: {len(test)}行 (陽性率 {y_test.mean():.3f})")
    print(f"学習件数: {len(train)}行")

    # --- (a) 指標単体でのAUC ---
    old_score = -test["current_meet_avg_finish_reconstructed"].to_numpy()
    new_score = -test["current_meet_avg_finish_adjusted_reconstructed"].to_numpy()

    auc_old_solo = roc_auc_score(y_test, old_score)
    auc_new_solo = roc_auc_score(y_test, new_score)
    print("\n[(a) 指標単体でのAUC]")
    print(f"  旧指標 current_meet_avg_finish        : AUC={auc_old_solo:.4f}")
    print(f"  新指標 current_meet_avg_finish_adjusted: AUC={auc_new_solo:.4f}")

    corr = test[[
        "current_meet_avg_finish_reconstructed",
        "current_meet_avg_finish_adjusted_reconstructed",
    ]].corr().iloc[0, 1]
    print(f"  新旧指標の相関係数: {corr:.4f}")

    # --- (b) 基礎特徴量+今節指標一本のモデルでの比較 ---
    results = {}
    for label, col in [
        ("旧指標(単純平均)", "current_meet_avg_finish_reconstructed"),
        ("新指標(コース補正)", "current_meet_avg_finish_adjusted_reconstructed"),
    ]:
        tr = train.copy()
        te = test.copy()
        tr["meet_feature"] = tr[col]
        te["meet_feature"] = te[col]

        model, num_cols = _pipeline()
        y_tr = (tr["finish"] == 1).astype(int)
        model.fit(tr[num_cols + BASE_CAT], y_tr)

        proba = model.predict_proba(te[num_cols + BASE_CAT])[:, 1]
        auc = roc_auc_score(y_test, proba)

        perm = permutation_importance(
            model,
            te[num_cols + BASE_CAT],
            y_test,
            scoring="roc_auc",
            n_repeats=20,
            random_state=0,
        )
        feat_names = num_cols + BASE_CAT
        meet_idx = feat_names.index("meet_feature")
        perm_mean = perm.importances_mean[meet_idx]
        perm_std = perm.importances_std[meet_idx]

        results[label] = (auc, perm_mean, perm_std)

    print("\n[(b) 基礎特徴量 + 今節指標一本のモデル比較]")
    for label, (auc, perm_mean, perm_std) in results.items():
        print(
            f"  {label}: モデル全体AUC={auc:.4f}  "
            f"今節指標のPermutation Importance(ΔAUC)="
            f"{perm_mean:+.5f} (std={perm_std:.5f})"
        )


if __name__ == "__main__":
    main()
