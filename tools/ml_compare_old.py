"""
今の本番モデル（prediction.predict）の事後予想を、資金配分まで含めて手元に保存する（新モデルとの比較用）。

hindcast.run_day と同じ入力（その日の時点で分かっていた情報）・同じ学習データの範囲で予想し、
本番と同じ手順で買い目（rank_tickets）と資金配分（allocate_stakes_smart）まで作る。
結果は公式データを含むので、非公開のデータ用リポジトリ側のフォルダ（--out）にだけ書く。

  python tools/ml_compare_old.py --start 20260707 --end 20261006 --out <データ用リポジトリ>/reports/...
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hindcast as hc  # noqa: E402
from data_paths import data_path  # noqa: E402
from prediction import predict, train  # noqa: E402
from stake_allocator import allocate_stakes_smart, ticket_hit_probability  # noqa: E402

# 本番の設定（Supabase の app_settings。hindcast_runs.settings と同じ値）
RUNTIME = {
    "main_n": 4, "cover_n": 4, "hole_n": 0, "total_budget": 2000, "min_bet": 100,
    "longshot_min_prob_pct": 0.3, "value_bias": 0.0, "prediction_style": "バランス",
    "display_weight": 0.32, "weather_weight": 0.1, "venue_course_weight": 0.12, "hedge_enabled": True,
}


def bet(final, race, runtime=RUNTIME):
    """hindcast.predict_race と同じ買い目に、本番と同じ資金配分を付ける。"""
    from prediction import (adaptive_ticket_plan, assess_favorite_risk, confidence, rank_tickets,
                            second_favorite_n_for, trifecta)

    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main = min(int(plan["main_n"]), target)
    favorite_lane, risk, _ = assess_favorite_risk(race, final)
    hedge = favorite_lane if runtime["hedge_enabled"] and risk >= 2 else None
    tickets = rank_tickets(
        tri, odds=None, main_n=main, cover_n=target - main, longshot_n=0,
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0, hedge_lane=hedge, use_odds=False,
        first=final, min_second_coverage=plan["min_second_coverage"], close_third_gap=None,
        close_third_coverage=4, second_favorite_n=second_favorite_n_for(final),
    )
    tickets = allocate_stakes_smart(
        tickets, budget=runtime["total_budget"], unit=100, min_bet=runtime["min_bet"],
        max_longshot_share=0.15, max_ticket_share=0.35, value_bias=runtime["value_bias"],
        use_odds=False, guarantee_col="second_favorite",
    )
    return {
        "tickets": tickets[["combo", "group", "prob", "stake"]].to_dict("records"),
        "hit_probability": ticket_hit_probability(tickets),
        "tri": dict(zip(tri["combo"], tri["prob"])),
        "confidence": confidence(final, race),
    }


def run(start, end, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    history_full = pd.read_csv(data_path("history_full.csv"), dtype=str, keep_default_na=False).replace({"": np.nan})
    history_full["race_date"] = history_full["race_date"].astype(str)
    sample_history = pd.read_csv(data_path("sample_history.csv"))
    sample_history["_d"] = pd.to_datetime(sample_history["race_date"], errors="coerce")
    days = [d.strftime("%Y%m%d") for d in pd.date_range(start, end)]
    k, _ = hc.load_k_results(days[0], days[-1], download_missing=False)

    for d in days:
        path = out / f"old_{d}.pkl"
        if path.exists():
            continue
        day = pd.Timestamp(d)
        hf_day = history_full[history_full["race_date"] == d]
        if hf_day.empty:
            continue
        t0 = time.time()
        model = train(sample_history[sample_history["_d"] < day].drop(columns="_d"), as_of=d)
        hf_before = history_full[history_full["race_date"] < d]
        cstats, vprof = hc.course_stats(k, day), hc.venue_profile(k, day)
        k_days = {
            jcd: g.groupby("d").agg(day_no=("day_no", "first"), meet_title=("meet_title", "first")).sort_index()
            for jcd, g in k[(k["d"] <= day) & (k["d"] >= day - pd.Timedelta(days=14))].groupby("jcd")
        }
        baseline = hc.course_baseline(hf_before)
        rows_out, races = {}, {}
        for race_key, rows in hf_day.groupby("race_key", sort=True):
            rows = rows.drop_duplicates("lane")
            if len(rows) < 3:
                continue
            jcd = str(rows["jcd"].iloc[0]).zfill(2)
            k_meet = k[(k["jcd"] == jcd) & k["d"].isin(hc.meet_days(k_days, jcd, day))]
            race = hc.build_race(rows, day, cstats, vprof, k_meet, baseline)
            try:
                final = predict(model, race, display_weight=RUNTIME["display_weight"],
                                weather_weight=RUNTIME["weather_weight"],
                                venue_course_weight=RUNTIME["venue_course_weight"], original_display_scale=0.0)
                res = bet(final, race)
            except Exception as e:  # noqa: BLE001
                print(f"[OLD] error {race_key}: {type(e).__name__}: {e}", flush=True)
                continue
            res["p_first"] = dict(zip(final["lane"].astype(int), final["p_first"].astype(float)))
            rows_out[race_key] = res
            # race（入力）は新モデルの買い目（危険度の判定）でも同じものを使うので一緒に残す
            races[race_key] = race
        with open(path, "wb") as fh:
            pickle.dump({"pred": rows_out, "race": races}, fh)
        print(f"[OLD] {d}: {len(rows_out)}レース {time.time() - t0:.0f}秒", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    run(a.start, a.end, a.out)
