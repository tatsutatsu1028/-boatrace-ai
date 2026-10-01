"""
2着・3着モデルの学習データを「補完前」と「補完後」で比べるバックテスト。

  補完前: history_full.csv のうち空白期間（2026-06-25〜09-07）を除いた行
          （補完前の 6,979 レースと同じ集合）
  補完後: history_full.csv 全体（空白期間を build_history_full.py で補完済み）

テスト: 2026-09-08 以降の全レース（history_full.csv にあるもの）。
テスト日ごとに、その日より前のレースだけで2着・3着モデルを学習し直す
（ウォークフォワード）。学習期間とテスト期間は重ならない。
1着モデルは本番と同じ sample_history.csv（〜2026-08-17）で、両パターン共通。

買い目は auto_random_fix._process_candidate と同じ手順・同じ設定値で作る
（adaptive_ticket_plan → rank_tickets(use_odds=False) → allocate_stakes_smart、
非推奨レースは賭け金0）。オッズは買い目選択に使わないので渡さない。

指標（result_tracker と同じ定義）:
  候補内的中率   : 実際の3連単が買い目一覧（賭け金0の候補も含む）に入っているか
  本命買い目的中 : 実際の3連単が買い目の1番目と一致するか
  回収率         : 払戻合計 / 賭け金合計（賭け金0のレースは分母に入らない）

  BOATRACE_DATA_DIR=<データ用リポジトリ> python backtest_position_history_fill.py

train()/predict() などの本番コードは一切変更しない。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed

import data_paths
import prediction
from prediction import (
    adaptive_ticket_plan,
    assess_favorite_risk,
    predict,
    rank_tickets,
    trifecta,
)
from stake_allocator import allocate_stakes_smart

GAP_START, GAP_END = "20260625", "20260907"
TEST_START = "20260908"

# app_settings id=1 の現在値（2026-10-01 時点。既定値と同じ）
RUNTIME = {
    "display_weight": 0.32,
    "weather_weight": 0.10,
    "venue_course_weight": 0.12,
    "hedge_enabled": True,
    "total_budget": 2000,
    "min_bet": 100,
    "longshot_min_prob_pct": 0.30,
    "value_bias": 0.0,
}


def _load_history():
    df = pd.read_csv(
        data_paths.data_path("history_full.csv"),
        dtype={"race_date": str, "jcd": str, "racer_id": str, "race_key": str},
    )
    return df


def _train(first_history, position_rows, workdir):
    """position_rows を history_full.csv として置いたフォルダを参照させて train() を呼ぶ。"""
    position_rows.to_csv(Path(workdir) / "history_full.csv", index=False)
    original_dir = os.environ.get(data_paths.ENV_NAME)
    os.environ[data_paths.ENV_NAME] = str(workdir)
    try:
        return prediction.train(first_history)
    finally:
        if original_dir is None:
            os.environ.pop(data_paths.ENV_NAME, None)
        else:
            os.environ[data_paths.ENV_NAME] = original_dir


def _tickets(model, race):
    rt = RUNTIME
    final = predict(
        model,
        race,
        display_weight=rt["display_weight"],
        weather_weight=rt["weather_weight"],
        venue_course_weight=rt["venue_course_weight"],
        original_display_scale=0.0,
    )
    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main_points = min(int(plan["main_n"]), target)
    favorite_lane, risk_score, _ = assess_favorite_risk(race, final)
    hedge_lane = favorite_lane if rt["hedge_enabled"] and risk_score >= 2 else None
    tickets = rank_tickets(
        tri,
        odds=None,
        main_n=main_points,
        cover_n=target - main_points,
        longshot_n=0,
        longshot_min_prob=rt["longshot_min_prob_pct"] / 100.0,
        hedge_lane=hedge_lane,
        use_odds=False,
        first=final,
        min_first_margin=0.40,
        min_second_coverage=plan["min_second_coverage"],
        close_third_gap=None,
        close_third_coverage=4,
        include_nonrecommended=True,
        second_favorite_n=2,
    )
    tickets = allocate_stakes_smart(
        tickets,
        budget=rt["total_budget"],
        unit=100,
        min_bet=rt["min_bet"],
        max_longshot_share=0.15,
        max_ticket_share=0.35,
        value_bias=rt["value_bias"],
        use_odds=False,
        guarantee_col="second_favorite",
    )
    if "recommended" in tickets.columns and not tickets["recommended"].fillna(True).all():
        tickets["stake"] = 0
    return tickets


def _evaluate_chunk(model, races):
    rows = []
    for race_key, race in races:
        race = race.sort_values("lane").reset_index(drop=True)
        actual = str(race["trifecta"].iloc[0]).strip()
        payout = float(pd.to_numeric(race["trifecta_payout_per_100"].iloc[0], errors="coerce") or 0.0)
        try:
            t = _tickets(model, race)
        except Exception as e:  # 1レースの不備で全体を止めない
            rows.append({"race_key": race_key, "error": f"{type(e).__name__}: {e}"})
            continue
        combos = t["combo"].astype(str).tolist()
        stake = pd.to_numeric(t["stake"], errors="coerce").fillna(0)
        hit_mask = t["combo"].astype(str) == actual
        stake_on_hit = float(stake[hit_mask].sum())
        rows.append({
            "race_key": race_key,
            "error": "",
            "points": len(combos),
            "candidate_hit": int(actual in combos),
            "top_hit": int(bool(combos) and combos[0] == actual),
            "top_combo": combos[0] if combos else "",
            "stake": float(stake.sum()),
            "payout": stake_on_hit * payout / 100.0,
        })
    return rows


def _evaluate(model, test, n_jobs):
    races = list(test.groupby("race_key", sort=False))
    k = max(1, min(len(races), n_jobs * 4))
    chunks = [races[i::k] for i in range(k)]
    parts = Parallel(n_jobs=n_jobs)(delayed(_evaluate_chunk)(model, c) for c in chunks)
    return pd.DataFrame([r for p in parts for r in p])


def _summary(res):
    ok = res[res["error"] == ""]
    staked = ok[ok["stake"] > 0]
    return {
        "レース数": len(ok),
        "候補内的中率": ok["candidate_hit"].mean(),
        "本命買い目的中率": ok["top_hit"].mean(),
        "賭けたレース数": len(staked),
        "賭け金合計": staked["stake"].sum(),
        "払戻合計": staked["payout"].sum(),
        "回収率": staked["payout"].sum() / staked["stake"].sum() if len(staked) else np.nan,
        "平均点数": ok["points"].mean(),
    }


def _bootstrap(a, b, n_boot=4000, seed=0):
    """同じレースを対応させたブートストラップで (後 - 前) の95%区間。"""
    m = a.merge(b, on="race_key", suffixes=("_a", "_b"))
    m = m[(m["error_a"] == "") & (m["error_b"] == "")]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(m), size=(n_boot, len(m)))
    out = {}
    for col in ("candidate_hit", "top_hit"):
        x, y = m[f"{col}_a"].to_numpy(float), m[f"{col}_b"].to_numpy(float)
        d = y[idx].mean(1) - x[idx].mean(1)
        out[col] = (y.mean() - x.mean(), *np.percentile(d, [2.5, 97.5]))
    sa, pa = m["stake_a"].to_numpy(float), m["payout_a"].to_numpy(float)
    sb, pb = m["stake_b"].to_numpy(float), m["payout_b"].to_numpy(float)
    d = pb[idx].sum(1) / sb[idx].sum(1) - pa[idx].sum(1) / sa[idx].sum(1)
    out["roi"] = (pb.sum() / sb.sum() - pa.sum() / sa.sum(), *np.percentile(d, [2.5, 97.5]))
    out["same_top"] = float((m["top_combo_a"] == m["top_combo_b"]).mean())
    return out, m


def main():
    n_jobs = int(os.environ.get("N_JOBS", os.cpu_count() or 2))
    hist = _load_history()
    first_history = pd.read_csv(data_paths.data_path("sample_history.csv"))
    in_gap = hist["race_date"].between(GAP_START, GAP_END)
    arms = {"補完前": hist[~in_gap], "補完後": hist}

    test_all = hist[hist["race_date"] >= TEST_START]
    days = sorted(test_all["race_date"].unique())
    print(f"テスト {days[0]}〜{days[-1]} {test_all['race_key'].nunique()}レース / {len(days)}日", flush=True)

    results = {name: [] for name in arms}
    with tempfile.TemporaryDirectory() as tmp:
        for day in days:
            test = test_all[test_all["race_date"] == day]
            for name, rows in arms.items():
                train_rows = rows[rows["race_date"] < day]
                assert train_rows["race_date"].max() < day
                wd = Path(tmp) / name
                wd.mkdir(exist_ok=True)
                model = _train(first_history, train_rows, wd)
                r = _evaluate(model, test, n_jobs)
                r["race_date"] = day
                r["train_races"] = train_rows["race_key"].nunique()
                results[name].append(r)
                print(f"  {day} {name}: 学習 {train_rows['race_key'].nunique()}R → テスト {len(r)}R", flush=True)

    res = {k: pd.concat(v, ignore_index=True) for k, v in results.items()}
    out_dir = Path(os.environ.get("BACKTEST_OUT", "."))
    for k, v in res.items():
        v.to_csv(out_dir / f"backtest_position_fill_{'before' if k == '補完前' else 'after'}.csv", index=False)

    print("=" * 60)
    summary = pd.DataFrame({k: _summary(v) for k, v in res.items()})
    print(summary.to_string())
    for k, v in res.items():
        n_err = int((v["error"] != "").sum())
        if n_err:
            print(f"{k}: エラー {n_err}レース 例: {v.loc[v['error'] != '', 'error'].iloc[0]}")

    diff, m = _bootstrap(res["補完前"], res["補完後"])
    print("=" * 60)
    print("差（補完後 − 補完前）と95%区間（同じレース対応のブートストラップ）")
    for key, label in (("candidate_hit", "候補内的中率"), ("top_hit", "本命買い目的中率"), ("roi", "回収率")):
        d, lo, hi = diff[key]
        print(f"  {label}: {d:+.4f}  [{lo:+.4f}, {hi:+.4f}]")
    print(f"  本命買い目が同じレースの割合: {diff['same_top']:.3f}")

    # 週ごとの内訳
    m["week"] = pd.to_datetime(m["race_key"].str[:8]).dt.to_period("W-SUN").astype(str)
    g = m.groupby("week")
    wk = pd.DataFrame({
        "レース": g.size(),
        "候補内_前": g["candidate_hit_a"].mean(), "候補内_後": g["candidate_hit_b"].mean(),
        "本命_前": g["top_hit_a"].mean(), "本命_後": g["top_hit_b"].mean(),
        "回収_前": g["payout_a"].sum() / g["stake_a"].sum(),
        "回収_後": g["payout_b"].sum() / g["stake_b"].sum(),
    })
    print("=" * 60)
    print(wk.round(3).to_string())


if __name__ == "__main__":
    sys.exit(main())
