"""
展示前の予想（展示・直前情報を使わないモデル）の検証。本番には使わない。

「展示前」= 当日の朝（1Rの展示より前）に分かっている情報だけ:
  - 番組表（Bファイル）と出走表（勝率・2連率・平均ST・F/L・級別・締切時刻・節の何日目 など）
  - 前日までの過去の集計（選手・選手×コース・選手×場・モーター・ST）
  - 今節成績は前日まで（当日の若いRの結果も使わない）
使わない: 展示タイム・展示ST・展示の進入（想定コースは艇番にする）・チルト・プロペラ/部品交換・
  調整重量・当日体重（直前情報）・気象と水面（直前情報・前のレースの値）・安定板

今の新モデル（ml-chain-v2-lgb-2y。reports/ml_v2 と同じ学習期間）と同じ手順で学習し、テスト3か月で
  1. 展示前モデルと展示後モデル（今の新モデル）の 1着的中率・候補内的中率・回収率
  2. 毎日、展示前モデルの「この買い目の的中確率」が高い順に10レース選んだときの成績
  3. 選んだレースのうち展示後に評価が大きく変わった割合
  4. 選び方の比較（的中確率の下限＋配当の見込み・期待値 など）
を出す。結果の表は公式データから計算したものなので、--out（非公開のデータ用リポジトリ）にだけ書く。
標準出力には進み具合だけを出す。

  BOATRACE_DATA_DIR=<データ用リポジトリ> python tools/ml_pre_exhibition.py \
      --out <データ用リポジトリ>/reports/pre_exhibition --post-model <データ用リポジトリ>/reports/ml_v2/models/ml-chain-v2-lgb-2y.joblib
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import history_store as store  # noqa: E402
import ml_features as mf  # noqa: E402
import ml_model as mm  # noqa: E402
from ml_experiment import (CALIB_START, TEST_END, TEST_START, actual_trifecta, bet_all,  # noqa: E402
                           finals_by_race, first_metrics, log, split)

# 展示・直前情報で分かる列（展示前モデルでは使わない）
PRE_DROP = [
    "exhibition_time", "exhibition_st", "tilt", "propeller_new", "parts_exchanged", "adjust_weight",
    "front_entry", "n_front_entry",
    "rk_exh_time", "rk_exh_st", "d_exh_time", "d_exh_st", "in_exh_st", "lane1_exh_st", "st12_ex_diff",
    "wind_speed", "wave_cm", "temperature", "water_temperature", "weather_code", "wind_rel", "wind_head",
    "wind_cross", "stabilizer",
]
# 番組表・出走表の並びに付く、展示・直前情報の元の列（欠損にしてから組み立てる）
_PRE_BLANK_BASE = [
    "exhibition_time", "p_exhibition_time", "exhibition_course", "exhibition_st", "tilt", "propeller_new",
    "parts_exchanged", "adjust_weight", "p_weight", "temperature", "water_temperature", "p_wind_speed",
    "p_wave_cm", "weather_code", "wind_direction_code", "stadium_direction_code", "stabilizer",
    "pw_wind_speed", "pw_wave_cm", "pw_weather",
]
PRE_FEATURES = [c for c in mf.FEATURES + mm.LANE_FREE_DERIVED if c not in PRE_DROP]
PRE_LANE_FREE = [c for c in mf.LANE_FREE_FEATURES if c not in PRE_DROP]


# ---------------------------------------------------------------
# 特徴量
# ---------------------------------------------------------------
def morning_meet(meet, e):
    """今節成績を「その日の朝」の値にする（その日の最初の走の、前日までの値をその日の全走に使う）。"""
    key = e[["race_key", "lane", "racer_id", "d", "race_no"]].copy()
    key["lane"] = key["lane"].astype(float)
    m = meet.copy()
    m["lane"] = m["lane"].astype(float)
    m = m.merge(key, on=["race_key", "lane"], how="left")
    vals = [c for c in m.columns if c not in ("race_key", "lane", "racer_id", "d", "race_no")]
    # 欠損を飛ばす first() は使わない（その日の2走目の値が1走目に混ざるため）
    m = m.sort_values(["racer_id", "d", "race_no"]).reset_index(drop=True)
    head = m.groupby(["racer_id", "d"], sort=False).cumcount() == 0
    morning = m.loc[head, ["racer_id", "d"] + vals]
    m = m[["race_key", "lane", "racer_id", "d"]].merge(morning, on=["racer_id", "d"], how="left")
    return m[["race_key", "lane"] + vals]


def build_tables(k, b, p):
    """展示後（今の新モデルと同じ）と展示前の特徴量の表を、同じ元データから作る。"""
    base = k[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "finish",
              "finish_raw", "exhibition_time", "motor_no", "distance_m", "fixed_entry",
              "race_type", "day_no"]].copy()
    base = base[~base["finish_raw"].astype(str).str.strip().str.startswith("K")].copy()
    base["finish"] = mf._num(base["finish"])
    base["exhibition_time"] = mf._num(base["exhibition_time"]).where(lambda s: s.between(6.0, 8.0))
    base = base.merge(mf._program_times(b), on="race_key", how="left")
    base = base.merge(b[["race_key", "lane"] + mf._B_COLS].drop_duplicates(["race_key", "lane"]),
                      on=["race_key", "lane"], how="left")
    pp = p[mf._PAGES_COLS].drop_duplicates(["race_key", "lane"]).rename(columns=mf._PAGES_RENAME)
    base = base.merge(pp, on=["race_key", "lane"], how="left")
    base = base.merge(mf.prev_race_weather(k), on="race_key", how="left")
    e = mf._events(k)
    state = mf.build_state(e)
    meet = mf.meet_features(e)
    t0 = time.time()
    post = mf.assemble(base, state, meet)
    log(f"[PRE] 展示後の表 {post.shape} {time.time() - t0:.0f}秒")
    pre_base = base.copy()
    for c in _PRE_BLANK_BASE:
        if c in pre_base.columns:
            pre_base[c] = np.nan
    pre = mf.assemble(pre_base, state, morning_meet(meet, e))
    log(f"[PRE] 展示前の表 {pre.shape} {time.time() - t0:.0f}秒")
    return post, pre


# ---------------------------------------------------------------
# 買い目
# ---------------------------------------------------------------
def races_for_bet(f):
    """本命の危険度（保険の買い目）の判定に使う入力。本番の race のうち判定に使う列だけ。"""
    out = {}
    cols = {"lane": "lane", "motor_2ren": "motor_2ren", "boat_2ren": "boat_2ren",
            "exhibition_time": "exhibition_time", "meet_top2": "current_meet_top2_rate"}
    x = f[["race_key"] + list(cols)].rename(columns=cols)
    x["lane"] = x["lane"].astype(int)
    for rk, g in x.groupby("race_key", sort=False):
        out[rk] = g.drop(columns="race_key").reset_index(drop=True)
    return out


def payouts(start, end):
    p = store.read_kind("k_payouts", start, end)
    p = p[p["bet_type"].astype(str).str.contains("3連単")].copy()
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    p["combo"] = p["combo"].astype(str).str.strip()
    return p.drop_duplicates("race_key").set_index("race_key")[["combo", "payout"]]


def race_rows(name, preds, actual, pay):
    rows = []
    for rk, r in preds.items():
        a = actual.get(rk)
        if a is None or rk not in pay.index:
            continue
        po = float(pay.at[rk, "payout"]) if pay.at[rk, "combo"] == a else np.nan
        tk = r["tickets"]
        stake = sum(float(t.get("stake") or 0) for t in tk)
        hit_stake = sum(float(t.get("stake") or 0) for t in tk if t["combo"] == a)
        fav = max(r["p_first"], key=r["p_first"].get)
        rows.append({
            "model": name, "race_key": rk, "race_date": rk[:8], "actual": a, "winner": int(a[0]),
            "payout": po, "favorite": fav, "p_favorite": r["p_first"][fav],
            "first_hit": fav == int(a[0]),
            "candidate_hit": a in [t["combo"] for t in tk],
            "bet_hit": hit_stake > 0,
            "n_tickets": len(tk), "stake": stake,
            "return": hit_stake * po / 100.0 if np.isfinite(po) else 0.0,
            "hit_probability": r["hit_probability"],
            "combos": " ".join(t["combo"] for t in tk),
            "_tickets": tk,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------
# 配当の見込み（オッズを使わず、展示前に分かる情報だけで当たったときの配当を見積もる）
# ---------------------------------------------------------------
def _combo_feats(tri):
    """tri: race_key, combo, prob（120通り）。組み番ごとの特徴量。"""
    t = tri.copy()
    t["a"] = t["combo"].str[0].astype(int)
    t["b"] = t["combo"].str[2].astype(int)
    t["c"] = t["combo"].str[4].astype(int)
    t["logp"] = np.log(np.clip(t["prob"], 1e-6, None))
    g = t.groupby("race_key")["prob"]
    t["rank"] = g.rank(ascending=False, method="first")
    t["p_top"] = g.transform("max")
    t["entropy"] = (-(t["prob"] * np.log(np.clip(t["prob"], 1e-12, None)))).groupby(t["race_key"]).transform("sum")
    return t


PAY_FEATS = ["logp", "a", "b", "c", "rank", "p_top", "entropy"]


def fit_payout_model(tri, pay):
    """確率の調整期間の「実際に来た組み番」の配当（対数）を、モデルの確率などから学習する。"""
    import lightgbm as lgb

    t = _combo_feats(tri)
    a = pay.reset_index().rename(columns={"combo": "combo"})
    x = t.merge(a, on=["race_key", "combo"])
    x = x[x["payout"] > 0]
    model = lgb.train({"objective": "regression", "learning_rate": 0.05, "num_leaves": 15,
                       "min_data_in_leaf": 50, "verbosity": -1, "seed": 0},
                      lgb.Dataset(x[PAY_FEATS], np.log(x["payout"])), num_boost_round=300)
    resid = np.log(x["payout"]) - model.predict(x[PAY_FEATS])
    smear = float(np.mean(np.exp(resid)))  # 対数で学習したものを金額に戻すときの補正
    return model, smear, len(x)


def expected_pay(model, smear, tri):
    t = _combo_feats(tri)
    t["exp_pay"] = np.exp(model.predict(t[PAY_FEATS])) * smear
    return t.set_index(["race_key", "combo"])["exp_pay"]


def add_pay_expectation(rows, exp):
    """買い目ごとの見込み配当から、当たったときの配当の見込み・期待回収率を足す。"""
    hitpay, ev = [], []
    for rk, tk in zip(rows["race_key"], rows["_tickets"]):
        ps = np.array([float(t["prob"]) for t in tk])
        st = np.array([float(t.get("stake") or 0) for t in tk])
        pay = np.array([exp.get((rk, t["combo"]), np.nan) for t in tk])
        hitpay.append(float(np.nansum(ps * pay) / ps.sum()) if ps.sum() else np.nan)
        ev.append(float(np.nansum(ps * st * pay / 100.0) / st.sum()) if st.sum() else np.nan)
    rows = rows.copy()
    rows["exp_hit_pay"] = hitpay  # 当たったときの配当の見込み（買い目の確率で重み付け、100円あたり）
    rows["exp_roi"] = ev          # 期待回収率の見込み（配当の見込み × 確率 × 賭け金）
    return rows


# ---------------------------------------------------------------
# 集計
# ---------------------------------------------------------------
def agg(d):
    hits = d[d["candidate_hit"]]
    return {
        "races": int(len(d)),
        "days": int(d["race_date"].nunique()),
        "first_hit": float(d["first_hit"].mean()),
        "candidate_hit": float(d["candidate_hit"].mean()),
        "bet_hit": float(d["bet_hit"].mean()),
        "roi": float(d["return"].sum() / d["stake"].sum()),
        "avg_payout_all": float(d["payout"].mean()),          # そのレースの3連単の配当（当たり外れに関係なく）
        "median_payout_all": float(d["payout"].median()),
        "avg_payout_hit": float(hits["payout"].mean()) if len(hits) else np.nan,  # 候補内で当たったときの配当
        "avg_return_hit": float(d.loc[d["bet_hit"], "return"].mean()) if d["bet_hit"].any() else np.nan,
        "shown_hit_probability": float(d["hit_probability"].mean()),
        "avg_tickets": float(d["n_tickets"].mean()),
    }


def boot_ci(d, stat, n=500, seed=0):
    """日を選び直すブートストラップの95%区間。"""
    rng = np.random.default_rng(seed)
    days = d["race_date"].unique()
    by = {k: g for k, g in d.groupby("race_date")}
    vals = []
    for _ in range(n):
        s = pd.concat([by[x] for x in rng.choice(days, len(days))])
        vals.append(stat(s))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def pick_daily(d, score, n=10, cond=None, ascending=False):
    x = d if cond is None else d[cond(d)]
    x = x.sort_values(["race_date", score], ascending=[True, ascending])
    return x.groupby("race_date", sort=False).head(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--post-model", required=True, help="今の新モデル（reports/ml_v2 の 2y と同じ学習期間）")
    ap.add_argument("--cache", default="", help="特徴量の表の置き場所（再実行を速くする）")
    ap.add_argument("--reuse", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else None
    t0 = time.time()

    if cache and (cache / "post.pkl").exists():
        post = pd.read_pickle(cache / "post.pkl")
        pre = pd.read_pickle(cache / "pre.pkl")
    else:
        post, pre = build_tables(*mf.load_sources())
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            post.to_pickle(cache / "post.pkl")
            pre.to_pickle(cache / "pre.pkl")
    log(f"[PRE] 特徴量 {time.time() - t0:.0f}秒")

    # 展示前モデル（今の新モデルの 2y と同じ期間の分け方）
    version = f"{mm.MODEL_FAMILY}-pre-v2-lgb-2y"
    mpath = out / "models" / f"{version}.joblib"
    train, calib, test_pre = split(pre, "2y")
    if args.reuse and mpath.exists():
        pre_model = mm.ChainModel.load(mpath)
    else:
        pre_model = mm.ChainModel(PRE_FEATURES, version, engine="lgb", lane_free=PRE_LANE_FREE)
        pre_model.fit(train, calib, log=log)
        pre_model.save(mpath)
    log(f"[PRE] 展示前モデル {time.time() - t0:.0f}秒")
    post_model = mm.ChainModel.load(args.post_model)
    _, _, test_post = split(post, "2y")

    # 1着確率の正確さ
    fm_pre, _ = first_metrics(pre_model.predict_tables(test_pre)[0], test_pre)
    fm_post, _ = first_metrics(post_model.predict_tables(test_post)[0], test_post)

    # 買い目（本番と同じ手順）
    pay = payouts(TEST_START, TEST_END)
    actual = actual_trifecta(test_post)
    rows = {}
    tris = {}
    for name, model, test in (("pre", pre_model, test_pre), ("post", post_model, test_post)):
        finals, _ = finals_by_race(model, test)
        preds = bet_all(finals, races_for_bet(test))
        rows[name] = race_rows(name, preds, actual, pay)
        tris[name] = mm.trifecta_table(*model.predict_tables(test))
        log(f"[PRE] {name} 買い目 {len(preds)}レース {time.time() - t0:.0f}秒")

    # 配当の見込み（確率の調整期間で学習。テスト期間の結果は見ない）
    cal_tri = mm.trifecta_table(*pre_model.predict_tables(calib))
    pay_model, smear, n_pay = fit_payout_model(cal_tri, payouts(CALIB_START, "20260706"))
    exp = expected_pay(pay_model, smear, tris["pre"])
    rows["pre"] = add_pay_expectation(rows["pre"], exp)
    log(f"[PRE] 配当の見込み {n_pay}レースで学習 {time.time() - t0:.0f}秒")

    common = set(rows["pre"]["race_key"]) & set(rows["post"]["race_key"])
    pre_r = rows["pre"][rows["pre"]["race_key"].isin(common)].reset_index(drop=True)
    post_r = rows["post"][rows["post"]["race_key"].isin(common)].reset_index(drop=True)

    # 展示前と展示後の比較（レース単位で横に並べる）
    both = pre_r.merge(post_r[["race_key", "favorite", "hit_probability", "candidate_hit", "bet_hit",
                               "stake", "return", "combos", "first_hit"]],
                       on="race_key", suffixes=("", "_post"))
    both["fav_changed"] = both["favorite"] != both["favorite_post"]
    both["hp_delta"] = both["hit_probability_post"] - both["hit_probability"]

    def overlap(a, b):
        sa, sb = set(a.split()), set(b.split())
        return len(sa & sb) / max(len(sa), 1)

    both["ticket_overlap"] = [overlap(a, b) for a, b in zip(both["combos"], both["combos_post"])]
    both["post_day_rank"] = both.groupby("race_date")["hit_probability_post"].rank(ascending=False, method="first")
    both["pre_day_rank"] = both.groupby("race_date")["hit_probability"].rank(ascending=False, method="first")
    # 「大きく変わった」: 本命が変わった、または展示後の的中確率が10ポイント以上下がった
    both["big_change"] = both["fav_changed"] | (both["hp_delta"] <= -0.10)

    # ---- 1. 全レースの比較 ----
    overall = pd.DataFrame([dict(model="展示前モデル", **agg(pre_r)), dict(model="展示後モデル（今の新モデル）", **agg(post_r))])
    overall["first_logloss"] = [fm_pre["first_logloss"], fm_post["first_logloss"]]
    overall["first_brier"] = [fm_pre["first_brier"], fm_post["first_brier"]]
    diff_ci = {}
    for key, fn in (("candidate_hit", lambda s: s["candidate_hit"].mean() - s["candidate_hit_post"].mean()),
                    ("first_hit", lambda s: s["first_hit"].mean() - s["first_hit_post"].mean()),
                    ("roi", lambda s: s["return"].sum() / s["stake"].sum() - s["return_post"].sum() / s["stake_post"].sum())):
        diff_ci[key] = {"diff": fn(both), "ci95": boot_ci(both, fn)}

    # ---- 2〜4. 毎日10レースの選び方 ----
    strategies = {
        "全レース（参考）": both,
        "A 的中確率が高い順": pick_daily(both, "hit_probability"),
        "B1 的中確率55%以上で配当の見込みが高い順": pick_daily(both, "exp_hit_pay", cond=lambda d: d["hit_probability"] >= 0.55),
        "B2 的中確率60%以上で配当の見込みが高い順": pick_daily(both, "exp_hit_pay", cond=lambda d: d["hit_probability"] >= 0.60),
        "B3 的中確率65%以上で配当の見込みが高い順": pick_daily(both, "exp_hit_pay", cond=lambda d: d["hit_probability"] >= 0.65),
        "B4 的中確率70%以上で配当の見込みが高い順": pick_daily(both, "exp_hit_pay", cond=lambda d: d["hit_probability"] >= 0.70),
        "C 期待回収率の見込みが高い順": pick_daily(both, "exp_roi"),
        "D1 的中確率55%以上で期待回収率の見込みが高い順": pick_daily(both, "exp_roi", cond=lambda d: d["hit_probability"] >= 0.55),
        "D2 的中確率60%以上で期待回収率の見込みが高い順": pick_daily(both, "exp_roi", cond=lambda d: d["hit_probability"] >= 0.60),
        "E 的中確率の上位30から配当の見込みが高い順": pick_daily(pick_daily(both, "hit_probability", n=30), "exp_hit_pay"),
        "F 本命の1着確率が高い順": pick_daily(both, "p_favorite"),
        "参考: 展示後モデルで的中確率が高い順（展示後に選べた場合）": pick_daily(both, "hit_probability_post"),
    }
    srows = []
    for name, d in strategies.items():
        r = {"strategy": name, **agg(d)}
        r["per_day"] = r["races"] / max(r["days"], 1)
        r["candidate_hit_ci95"] = boot_ci(d, lambda s: s["candidate_hit"].mean())
        r["roi_ci95"] = boot_ci(d, lambda s: s["return"].sum() / s["stake"].sum())
        r["exp_hit_pay_mean"] = float(d["exp_hit_pay"].mean())
        # 展示後に評価が大きく変わった割合
        r["big_change"] = float(d["big_change"].mean())
        r["fav_changed"] = float(d["fav_changed"].mean())
        r["hp_drop_10pt"] = float((d["hp_delta"] <= -0.10).mean())
        r["hp_rise_10pt"] = float((d["hp_delta"] >= 0.10).mean())
        r["hp_delta_mean"] = float(d["hp_delta"].mean())
        r["ticket_overlap_mean"] = float(d["ticket_overlap"].mean())
        r["still_post_top10"] = float((d["post_day_rank"] <= 10).mean())
        r["still_post_top30"] = float((d["post_day_rank"] <= 30).mean())
        # 展示前に選んで、買い目は展示後モデルで作り直した場合
        r["post_candidate_hit"] = float(d["candidate_hit_post"].mean())
        r["post_roi"] = float(d["return_post"].sum() / d["stake_post"].sum())
        # 大きく変わったレースを外した場合（展示後に見直す運用）
        k = d[~d["big_change"]]
        r["kept_races_per_day"] = len(k) / max(r["days"], 1)
        r["kept_candidate_hit"] = float(k["candidate_hit"].mean()) if len(k) else np.nan
        r["kept_roi"] = float(k["return"].sum() / k["stake"].sum()) if len(k) else np.nan
        r["dropped_candidate_hit"] = float(d.loc[d["big_change"], "candidate_hit"].mean()) if d["big_change"].any() else np.nan
        # 月の前半（7〜8月）と後半（9〜10月）で同じ傾向か
        for half, m in (("jul_aug", d["race_date"] < "20260901"), ("sep_oct", d["race_date"] >= "20260901")):
            h = d[m]
            r[f"{half}_candidate_hit"] = float(h["candidate_hit"].mean()) if len(h) else np.nan
            r[f"{half}_roi"] = float(h["return"].sum() / h["stake"].sum()) if len(h) else np.nan
        srows.append(r)
    strat = pd.DataFrame(srows)

    # 的中確率の帯ごと（展示前モデル）
    bands = pd.cut(both["hit_probability"], [0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 1.01], right=False)
    band = both.groupby(bands, observed=True).apply(lambda d: pd.Series(agg(d) | {
        "big_change": d["big_change"].mean(), "exp_hit_pay": d["exp_hit_pay"].mean()}), include_groups=False)
    band = band.reset_index().rename(columns={"hit_probability": "band"})
    band["band"] = band["band"].astype(str)

    # 書き出し（非公開のデータ用リポジトリだけ）
    overall.to_csv(out / "overall.csv", index=False, encoding="utf-8")
    strat.to_csv(out / "strategies.csv", index=False, encoding="utf-8")
    band.to_csv(out / "hit_probability_bands.csv", index=False, encoding="utf-8")
    both.drop(columns=["_tickets"]).to_csv(out / "race_level.csv.gz", index=False, encoding="utf-8")
    summary = {
        "periods": {"train_to": "20260606", "calib": [CALIB_START, "20260706"], "test": [TEST_START, TEST_END]},
        "pre_model": dict(pre_model.info, temps=pre_model.temps, **fm_pre),
        "post_model": dict(post_model.info, version=post_model.version, temps=post_model.temps, **fm_post),
        "pre_features": PRE_FEATURES, "pre_dropped": PRE_DROP,
        "diff_pre_minus_post": diff_ci, "payout_model_races": n_pay, "races": len(both),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    log(f"[PRE] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
