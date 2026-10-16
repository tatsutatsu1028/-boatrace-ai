"""
「万舟チャンス」（3連単の払戻が100円あたり1万円以上になりやすいレース）の検証。本番には使わない。

  rates : 過去データ全体の万舟率（全体・場別・レース番号別・グレード別・月別など）
  eval  : レースごとに万舟になる確率を出すモデル（展示前版・展示後版）を作り、テスト期間で
          上位のレースの万舟率・期間の前半/後半の安定性・印を付けたレースの買い目の回収率を出す

結果の表・レース別のデータは公式データから計算したものなので、--out（非公開のデータ用リポジトリ）
にだけ書く。標準出力には進み具合だけを出す。

  BOATRACE_DATA_DIR=<データ用リポジトリ> python tools/manshu_eval.py rates --out <データ用リポジトリ>/reports/manshu
  BOATRACE_DATA_DIR=<データ用リポジトリ> python tools/manshu_eval.py eval --out <データ用リポジトリ>/reports/manshu \
      --cache <作業用フォルダ>
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

MANSHU_YEN = 10000  # 100円あたりの3連単の払戻がこれ以上なら万舟


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------
# 万舟かどうか（全レース）
# ---------------------------------------------------------------
def trifecta_payouts(start=None, end=None):
    """1レース1行: 3連単の払戻（同着で複数あるときは最大）。払戻の無いレース（不成立・返還）は入らない。"""
    p = store.read_kind("k_payouts", start, end)
    p = p[p["bet_type"].astype(str).str.strip() == "3連単"].copy()
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    p["popularity"] = pd.to_numeric(p["popularity"], errors="coerce")
    p = p[p["payout"] > 0]
    p = p.sort_values("payout", ascending=False).drop_duplicates("race_key")
    p["combo"] = p["combo"].astype(str).str.strip()
    return p[["race_key", "combo", "payout", "popularity"]].set_index("race_key")


def race_info(start=None, end=None):
    """1レース1行: 日付・場・R・節の何日目・レース種別・グレード（出走表ページの値。無い日は同じ節の値）。"""
    k = store.read_kind("k_results", start, end, columns=[
        "race_date", "jcd", "venue", "race_no", "race_key", "meet_title", "day_no", "race_type"])
    r = k.drop_duplicates("race_key").copy()
    pg = store.read_kind("pages", start, end, columns=["race_key", "jcd", "meet_title", "grade"])
    pg = pg.drop_duplicates("race_key")
    r = r.merge(pg[["race_key", "grade"]], on="race_key", how="left")
    # ページの無い日: 同じ場・同じ節名でページのある日の値
    by_meet = pg.dropna(subset=["grade"]).drop_duplicates(["jcd", "meet_title"]).set_index(["jcd", "meet_title"])["grade"]
    miss = r["grade"].isna()
    r.loc[miss, "grade"] = [by_meet.get((j, m)) for j, m in zip(r.loc[miss, "jcd"], r.loc[miss, "meet_title"])]
    r["grade"] = r["grade"].fillna("不明")
    r["race_no"] = pd.to_numeric(r["race_no"]).astype(int)
    r["jcd"] = r["jcd"].astype(str).str.zfill(2)
    return r


def labeled_races(start=None, end=None):
    r = race_info(start, end)
    pay = trifecta_payouts(start, end)
    r = r[r["race_key"].isin(pay.index)].copy()
    r["payout"] = pay.loc[r["race_key"], "payout"].to_numpy()
    r["popularity"] = pay.loc[r["race_key"], "popularity"].to_numpy()
    r["combo"] = pay.loc[r["race_key"], "combo"].to_numpy()
    r["manshu"] = r["payout"] >= MANSHU_YEN
    return r


def rate_table(r, by):
    g = r.groupby(by, observed=True)
    t = g.agg(races=("manshu", "size"), manshu=("manshu", "sum"), manshu_rate=("manshu", "mean"),
              avg_payout=("payout", "mean"), median_payout=("payout", "median"))
    t["lift"] = t["manshu_rate"] / r["manshu"].mean()
    return t.reset_index()


def cmd_rates(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    r = labeled_races(args.start or None, args.end or None)
    r["month"] = r["race_date"].str[:6]
    r["race_type_group"] = race_type_group(r["race_type"])
    r["combo_head"] = r["combo"].str[0]
    overall = {
        "from": r["race_date"].min(), "to": r["race_date"].max(), "races": int(len(r)),
        "days": int(r["race_date"].nunique()),
        "manshu": int(r["manshu"].sum()), "manshu_rate": float(r["manshu"].mean()),
        "avg_payout": float(r["payout"].mean()), "median_payout": float(r["payout"].median()),
        "payout_quantiles": {str(q): float(r["payout"].quantile(q)) for q in (0.5, 0.75, 0.9, 0.95)},
        "grade_from_pages_share": float((r["grade"] != "不明").mean()),
    }
    tables = {
        "venue": rate_table(r, ["jcd", "venue"]),
        "race_no": rate_table(r, "race_no"),
        "grade": rate_table(r, "grade"),
        "race_type": rate_table(r, "race_type_group"),
        "month": rate_table(r, "month"),
        "winner_lane": rate_table(r, "combo_head"),
        "venue_x_race_no": rate_table(r, ["venue", "race_no"]),
    }
    for name, t in tables.items():
        t.to_csv(out / f"rates_{name}.csv", index=False, encoding="utf-8")
    (out / "rates_overall.json").write_text(json.dumps(overall, ensure_ascii=False, indent=1, default=float))
    log(f"[MANSHU] rates {len(r)}レース")


def race_type_group(s):
    s = s.astype(str)
    out = pd.Series("その他（特選・選抜など）", index=s.index)
    out[s.str.startswith("予選")] = "予選"
    out[s.str.startswith("一般")] = "一般"
    out[s.str.contains("準優")] = "準優勝戦"
    out[s.str.fullmatch("優勝戦")] = "優勝戦"
    out[s.str.contains("ドリーム")] = "ドリーム戦"
    return out


# ---------------------------------------------------------------
# モデル（eval）
# ---------------------------------------------------------------
# テスト期間: 本番と同じ学習済みの新モデル（学習 〜2026-06-06・温度 06-07〜07-06）の予測を使う
TEST_START = "20260707"
# 万舟モデルの学習期間: 新モデルを日付で区切って学習し直し、学習に使っていない期間の予測を付ける
OOF_FOLDS = [
    # (新モデルの学習の終わり（この日を含まない）, 温度の期間の始め, 予測する期間の始め, 終わり)
    ("20250701", "20250701", "20250801", "20251130"),
    ("20251101", "20251101", "20251201", "20260331"),
    ("20260301", "20260301", "20260401", "20260706"),
]
TRAIN_START = OOF_FOLDS[0][2]

# 1艇ずつの列のうち、艇番ごとに横に並べてレースの特徴量にするもの
LANE_COLS_COMMON = ["class_ord", "national_win_rate", "r365_win", "rc_win", "rc_top3", "avg_st", "r180_st",
                    "motor_2ren", "mot_top2", "f_count", "rv_win"]
LANE_COLS_POST = ["exhibition_time", "exhibition_st", "rk_exh_time"]
RACE_COLS_COMMON = ["jcd", "race_no", "distance_m", "fixed_entry", "race_type_ord", "grade_ord", "day_no",
                    "is_final_day", "deadline_min", "is_night", "n_boats", "st12_diff", "outer_win_sum",
                    "outer_win_max", "outer_rc_win_sum"]
RACE_COLS_POST = ["wind_speed", "wave_cm", "wind_head", "wind_cross", "temperature", "water_temperature",
                  "weather_code", "stabilizer", "st12_ex_diff", "n_front_entry"]


def chain_summary(first, tri):
    """新モデルの1着確率・3連単確率の散らばり（1レース1行）。"""
    f = first.copy()
    f["lane"] = f["lane"].astype(int)
    p = f.pivot_table(index="race_key", columns="lane", values="p_first").reindex(columns=range(1, 7))
    p = p.fillna(0.0)
    arr = p.to_numpy()
    srt = -np.sort(-arr, axis=1)
    ent = -(arr * np.log(np.clip(arr, 1e-12, None))).sum(axis=1)
    out = pd.DataFrame(index=p.index)
    for ln in range(1, 7):
        out[f"c_p1_l{ln}"] = arr[:, ln - 1]
    out["c_p1_max"] = srt[:, 0]
    out["c_p1_2nd"] = srt[:, 1]
    out["c_p1_gap"] = srt[:, 0] - srt[:, 1]
    out["c_p1_entropy"] = ent
    out["c_p1_fav_lane"] = arr.argmax(axis=1) + 1
    out["c_p1_out456"] = arr[:, 3:].sum(axis=1)

    t = tri.copy()
    t["a"] = t["combo"].str[0].astype(int)
    t["in3_1"] = t["combo"].str.contains("1")
    t["n_out"] = t["combo"].str.count("[456]")
    g = t.groupby("race_key")
    out["c_tri_max"] = g["prob"].max()
    out["c_tri_entropy"] = g["prob"].apply(lambda s: float(-(s * np.log(np.clip(s, 1e-12, None))).sum()))
    t = t.sort_values(["race_key", "prob"], ascending=[True, False])
    t["rank"] = t.groupby("race_key").cumcount()
    out["c_tri_top5"] = t[t["rank"] < 5].groupby("race_key")["prob"].sum()
    out["c_tri_top10"] = t[t["rank"] < 10].groupby("race_key")["prob"].sum()
    out["c_tri_top20"] = t[t["rank"] < 20].groupby("race_key")["prob"].sum()
    out["c_lane1_out3"] = 1.0 - t[t["in3_1"]].groupby("race_key")["prob"].sum()
    out["c_two_outer"] = t[t["n_out"] >= 2].groupby("race_key")["prob"].sum()
    return out


def race_features(f, post):
    """1艇1行の特徴量の表から、1レース1行の特徴量（艇番ごとに横に並べる＋レースの列）。"""
    lane_cols = LANE_COLS_COMMON + (LANE_COLS_POST if post else [])
    race_cols = RACE_COLS_COMMON + (RACE_COLS_POST if post else [])
    x = f[["race_key", "lane"] + lane_cols].copy()
    x["lane"] = x["lane"].astype(int)
    w = x.pivot_table(index="race_key", columns="lane", values=lane_cols, dropna=False)
    w.columns = [f"{c}_l{ln}" for c, ln in w.columns]
    r = f.drop_duplicates("race_key").set_index("race_key")[race_cols + ["race_date"]]
    r = r.join(w)
    # 1号艇の強さ・艇同士の差
    for c in ("national_win_rate", "class_ord", "rc_win", "avg_st", "motor_2ren"):
        cols = [f"{c}_l{ln}" for ln in range(1, 7) if f"{c}_l{ln}" in r.columns]
        v = r[cols].apply(pd.to_numeric, errors="coerce")
        others = v.iloc[:, 1:]
        r[f"{c}_sd"] = v.std(axis=1)
        r[f"{c}_range"] = v.max(axis=1) - v.min(axis=1)
        r[f"{c}_l1_minus_othermax"] = v.iloc[:, 0] - others.max(axis=1)
        r[f"{c}_l1_minus_othermean"] = v.iloc[:, 0] - others.mean(axis=1)
    cls = r[[f"class_ord_l{ln}" for ln in range(1, 7)]].apply(pd.to_numeric, errors="coerce")
    top = np.nanmax(cls.to_numpy(), axis=1)
    r["n_top_class"] = (cls.to_numpy() == top[:, None]).sum(axis=1)
    if post:
        ex = r[[f"exhibition_time_l{ln}" for ln in range(1, 7)]].apply(pd.to_numeric, errors="coerce")
        r["exh_sd"] = ex.std(axis=1)
        r["exh_range"] = ex.max(axis=1) - ex.min(axis=1)
        r["exh_l1_minus_best_other"] = ex.iloc[:, 0] - ex.iloc[:, 1:].min(axis=1)
        r["exh_best_lane"] = ex.to_numpy().argmin(axis=1) + 1
        r.loc[ex.isna().all(axis=1), "exh_best_lane"] = np.nan
    r["jcd"] = pd.to_numeric(r["jcd"], errors="coerce")
    return r


def fold_models(table, version_tag, features, lane_free, cache):
    """OOF_FOLDS ごとに新モデルを学習し直し、予測する期間の1着確率・3連単確率を返す。"""
    import ml_model as mm

    firsts, tris = [], []
    for tr_end, cal_start, p_start, p_end in OOF_FOLDS:
        path = cache / f"fold_{version_tag}_{p_start}.joblib"
        if path.exists():
            model = mm.ChainModel.load(path)
        else:
            train = table[table["race_date"] < cal_start]
            calib = table[(table["race_date"] >= cal_start) & (table["race_date"] < p_start)]
            model = mm.ChainModel(features, f"fold-{version_tag}-{p_start}", engine="lgb", lane_free=lane_free)
            t0 = time.time()
            model.fit(train, calib, log=lambda *a: None)
            model.save(path)
            log(f"[MANSHU] fold {version_tag} {p_start} 学習 {time.time() - t0:.0f}秒")
        part = table[(table["race_date"] >= p_start) & (table["race_date"] <= p_end)]
        first, second, third = model.predict_tables(part)
        firsts.append(first)
        tris.append(mm.trifecta_table(first, second, third))
    return pd.concat(firsts, ignore_index=True), pd.concat(tris, ignore_index=True)


def chain_outputs(table, kind, args, cache):
    """学習期間（OOF）とテスト期間（本番と同じ学習済みモデル）の1着確率・3連単確率。"""
    import ml_features as mf
    import ml_model as mm
    from ml_pre_exhibition import PRE_FEATURES, PRE_LANE_FREE

    path = cache / f"chain_{kind}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    if kind == "post":
        feats, lf, mpath = mf.FEATURES + mm.LANE_FREE_DERIVED, mf.LANE_FREE_FEATURES, args.post_model
    else:
        feats, lf, mpath = PRE_FEATURES, PRE_LANE_FREE, args.pre_model
    f_oof, t_oof = fold_models(table, kind, feats, lf, cache)
    model = mm.ChainModel.load(mpath)
    test = table[table["race_date"] >= TEST_START]
    first, second, third = model.predict_tables(test)
    tri = mm.trifecta_table(first, second, third)
    res = {"first": pd.concat([f_oof, first], ignore_index=True),
           "tri": pd.concat([t_oof, tri], ignore_index=True)}
    pd.to_pickle(res, path)
    log(f"[MANSHU] {kind} 新モデルの出力 {res['first']['race_key'].nunique()}レース")
    return res


MANSHU_PARAMS = {"objective": "binary", "learning_rate": 0.02, "num_leaves": 15, "min_data_in_leaf": 200,
                 "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0,
                 "verbosity": -1, "seed": 0}


def fit_manshu(x, y, dates, cols):
    """万舟かどうかの2値分類（LightGBM）。学習期間の直近10%の日付で打ち切りの回数を決め、全期間で学び直す。"""
    import lightgbm as lgb

    cut = np.quantile(pd.to_numeric(dates).to_numpy(), 0.9)
    va = pd.to_numeric(dates).to_numpy() > cut
    cat = [c for c in ("jcd",) if c in cols]
    dtr = lgb.Dataset(x.loc[~va, cols], y[~va], categorical_feature=cat, free_raw_data=False)
    dva = lgb.Dataset(x.loc[va, cols], y[va], reference=dtr, categorical_feature=cat, free_raw_data=False)
    b = lgb.train(MANSHU_PARAMS, dtr, num_boost_round=3000, valid_sets=[dva],
                  callbacks=[lgb.early_stopping(150, verbose=False)])
    n = max(int(b.best_iteration * 1.1), 50)
    full = lgb.train(MANSHU_PARAMS, lgb.Dataset(x[cols], y, categorical_feature=cat), num_boost_round=n)
    return full, n


def auc(y, s):
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, s))


def boot_ci(d, stat, n=500, seed=0):
    """日を選び直すブートストラップの95%区間。"""
    rng = np.random.default_rng(seed)
    days = d["race_date"].unique()
    by = {k: g for k, g in d.groupby("race_date")}
    vals = [stat(pd.concat([by[x] for x in rng.choice(days, len(days))])) for _ in range(n)]
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def picks(d, score, how):
    """印を付けるレース。how: day10 / day20 / dayfrac0.1 / thr:<値>"""
    if how.startswith("day") and how[3:].isdigit():
        n = int(how[3:])
        x = d.sort_values(["race_date", score], ascending=[True, False])
        return x.groupby("race_date", sort=False).head(n)
    if how.startswith("dayfrac"):
        q = float(how[7:])
        rk = d.groupby("race_date")[score].rank(ascending=False, pct=True)
        return d[rk <= q]
    if how.startswith("thr:"):
        return d[d[score] >= float(how[4:])]
    raise ValueError(how)


# 穴寄りの買い目（オッズを使わない。モデルの3連単確率から作る）
def ticket_sets(tri, first):
    """1レースごとの買い目（100円ずつ）: 本命を1着から外した上位10点 / 1号艇を1着から外した上位10点。"""
    t = tri.copy()
    fav = first.loc[first.groupby("race_key")["p_first"].idxmax(), ["race_key", "lane"]]
    fav = fav.set_index("race_key")["lane"].astype(int)
    t["a"] = t["combo"].str[0].astype(int)
    t["fav"] = t["race_key"].map(fav)
    t = t.sort_values(["race_key", "prob"], ascending=[True, False])
    out = {}
    nofav = t[t["a"] != t["fav"]]
    out["穴: 本命を1着から外した上位10点"] = nofav.groupby("race_key").head(10)
    no1 = t[t["a"] != 1]
    out["穴: 1号艇を1着から外した上位10点"] = no1.groupby("race_key").head(10)
    out["参考: 3連単確率の上位10点"] = t.groupby("race_key").head(10)
    return out


def ticket_returns(sel, labels):
    """sel: race_key, combo（100円ずつ）。1レース1行の賭け金・払戻。"""
    s = sel[["race_key", "combo"]].merge(labels[["race_key", "combo", "payout"]], on=["race_key", "combo"],
                                         how="left")
    s["ret"] = s["payout"].fillna(0.0)
    g = s.groupby("race_key").agg(stake=("combo", "size"), ret=("ret", "sum"))
    g["stake"] *= 100.0
    return g


def cmd_eval(args):
    import ml_features as mf  # noqa: F401

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    post = pd.read_pickle(cache / "post.pkl")
    pre = pd.read_pickle(cache / "pre.pkl")
    lab = labeled_races(TRAIN_START, args.test_end)
    lab = lab[lab["race_date"] <= args.test_end]
    log(f"[MANSHU] 表 {time.time() - t0:.0f}秒")

    res = {}
    importance = []
    preds_all = {}
    for kind, table in (("pre", pre), ("post", post)):
        table = table[table["race_date"] <= args.test_end]
        ch = chain_outputs(table, kind, args, cache)
        cs = chain_summary(ch["first"], ch["tri"])
        rf = race_features(table[table["race_date"] >= TRAIN_START], post=(kind == "post"))
        x = rf.join(cs, how="inner")
        x = x.join(lab.set_index("race_key")[["manshu", "payout", "venue", "grade", "combo"]], how="inner")
        x["race_date"] = x["race_date"].astype(str)
        x = x.reset_index().rename(columns={"index": "race_key"})
        y = x["manshu"].astype(int).to_numpy()
        tr = (x["race_date"] < TEST_START).to_numpy()
        te = ~tr
        chain_cols = [c for c in cs.columns]
        base_cols = [c for c in rf.columns if c != "race_date"]
        variants = {
            "full": base_cols + chain_cols,
            "no_chain": base_cols,
            "chain_only": chain_cols + ["jcd", "race_no"],
        }
        for vname, cols in variants.items():
            m, n_iter = fit_manshu(x[tr], y[tr], x.loc[tr, "race_date"], cols)
            x[f"p_{vname}"] = m.predict(x[cols])
            res[f"{kind}_{vname}"] = {"n_iter": n_iter, "n_features": len(cols),
                                      "test_auc": auc(y[te], x.loc[te, f"p_{vname}"]),
                                      "train_rows": int(tr.sum()), "test_rows": int(te.sum())}
            if vname == "full":
                imp = pd.Series(m.feature_importance("gain"), index=cols).sort_values(ascending=False)
                importance.append(imp.rename(kind))
                # 印の基準（学習期間の上位10%・20%の値。本番で日を問わず使う絶対値の基準）
                res[f"{kind}_{vname}"]["train_q90"] = float(np.quantile(x.loc[tr, "p_full"], 0.9))
                res[f"{kind}_{vname}"]["train_q80"] = float(np.quantile(x.loc[tr, "p_full"], 0.8))
                (out / "models").mkdir(exist_ok=True)
                m.save_model(str(out / "models" / f"manshu-{kind}.txt"))
            log(f"[MANSHU] {kind} {vname} AUC {res[f'{kind}_{vname}']['test_auc']:.3f} {time.time() - t0:.0f}秒")
        # 単純な目安（学習なし）
        for s in ("c_p1_entropy", "c_tri_entropy"):
            res[f"{kind}_score_{s}"] = {"test_auc": auc(y[te], x.loc[te, s])}
        res[f"{kind}_score_1-p1_l1"] = {"test_auc": auc(y[te], -x.loc[te, "c_p1_l1"])}
        preds_all[kind] = (x, ch)
        log(f"[MANSHU] {kind} 完了 {time.time() - t0:.0f}秒")

    evaluate(preds_all, res, lab, out, args)
    pd.concat(importance, axis=1).to_csv(out / "importance.csv", encoding="utf-8")
    log(f"[MANSHU] 終了 {time.time() - t0:.0f}秒")


def evaluate(preds_all, res, lab, out, args):
    hows = ["day10", "day20", "dayfrac0.1", "dayfrac0.2"]
    rows, stab, rois, calib = [], [], [], []
    base_rate = None
    lab_k = lab.set_index("race_key")
    for kind, (x, ch) in preds_all.items():
        te = x[x["race_date"] >= TEST_START].copy()
        base_rate = float(te["manshu"].mean())
        mid = sorted(te["race_date"].unique())[len(te["race_date"].unique()) // 2]
        scores = {"モデル（全部の特徴量）": "p_full", "モデル（新モデルの出力なし）": "p_no_chain",
                  "モデル（新モデルの出力だけ＋場・R）": "p_chain_only", "1着確率のエントロピーだけ": "c_p1_entropy"}
        thr = {"thr:q90": res[f"{kind}_full"]["train_q90"], "thr:q80": res[f"{kind}_full"]["train_q80"]}
        for sname, col in scores.items():
            # 学習期間の基準値はモデル（全部の特徴量）の確率の値なので、そのモデルにだけ使う
            for how in hows + (list(thr) if col == "p_full" else []):
                h = how if not how.startswith("thr:q") else f"thr:{thr[how]}"
                d = picks(te, col, h)
                r = {"version": kind, "score": sname, "pick": how, "races": len(d),
                     "per_day": len(d) / te["race_date"].nunique(),
                     "manshu_rate": float(d["manshu"].mean()), "lift": float(d["manshu"].mean()) / base_rate,
                     "base_rate": base_rate, "avg_payout": float(d["payout"].mean()),
                     "median_payout": float(d["payout"].median())}
                r["manshu_rate_ci95"] = boot_ci(d, lambda s: s["manshu"].mean())
                for half, m in (("first_half", d["race_date"] < mid), ("second_half", d["race_date"] >= mid)):
                    hb = te[te["race_date"] < mid] if half == "first_half" else te[te["race_date"] >= mid]
                    r[f"{half}_manshu_rate"] = float(d.loc[m, "manshu"].mean())
                    r[f"{half}_base_rate"] = float(hb["manshu"].mean())
                    r[f"{half}_lift"] = r[f"{half}_manshu_rate"] / r[f"{half}_base_rate"]
                rows.append(r)
        # 月ごとの安定性（毎日10レース・モデル）
        d = picks(te, "p_full", "day10")
        te["month"] = te["race_date"].str[:6]
        d = d.assign(month=d["race_date"].str[:6])
        for mo, g in te.groupby("month"):
            dm = d[d["month"] == mo]
            stab.append({"version": kind, "month": mo, "races": len(g), "base_rate": g["manshu"].mean(),
                         "picked": len(dm), "picked_rate": dm["manshu"].mean(),
                         "lift": dm["manshu"].mean() / g["manshu"].mean()})
        # 予測確率の帯ごと
        b = pd.cut(te["p_full"], [0, 0.08, 0.12, 0.16, 0.2, 0.25, 0.3, 0.35, 0.4, 1.0], right=False)
        c = te.groupby(b, observed=True).agg(races=("manshu", "size"), shown=("p_full", "mean"),
                                             actual=("manshu", "mean")).reset_index()
        c["p_full"] = c["p_full"].astype(str)
        calib.append(c.assign(version=kind))

        # 回収率（テスト期間）
        tri = ch["tri"][ch["tri"]["race_key"].isin(set(te["race_key"]))]
        first = ch["first"][ch["first"]["race_key"].isin(set(te["race_key"]))]
        sets = ticket_sets(tri, first)
        labs = lab[lab["race_key"].isin(set(te["race_key"]))]
        tk = {name: ticket_returns(sel, labs) for name, sel in sets.items()}
        main_t = main_tickets(kind, args, te, labs)
        if main_t is not None:
            tk = {"本線: モデルの買い目（本番と同じ手順・2,000円）": main_t, **tk}
        marked = {
            "全レース": te,
            "印なし（毎日10レース以外）": te[~te["race_key"].isin(set(picks(te, "p_full", "day10")["race_key"]))],
            "印: 毎日10レース": picks(te, "p_full", "day10"),
            "印: 毎日20レース": picks(te, "p_full", "day20"),
            "印: その日の上位10%": picks(te, "p_full", "dayfrac0.1"),
            "印: 学習期間の上位10%の基準以上": picks(te, "p_full", f"thr:{thr['thr:q90']}"),
        }
        for tname, g in tk.items():
            g = g.join(lab_k[["race_date"]])
            for mname, d in marked.items():
                s = g[g.index.isin(set(d["race_key"]))]
                if not len(s):
                    continue
                hit = s["ret"] > 0
                r = {"version": kind, "tickets": tname, "races_set": mname, "races": len(s),
                     "stake": float(s["stake"].sum()), "return": float(s["ret"].sum()),
                     "roi": float(s["ret"].sum() / s["stake"].sum()), "hit_rate": float(hit.mean()),
                     "avg_hit_return": float(s.loc[hit, "ret"].mean()) if hit.any() else np.nan}
                r["roi_ci95"] = boot_ci(s, lambda q: q["ret"].sum() / q["stake"].sum())
                for half, m in (("first_half", s["race_date"] < mid), ("second_half", s["race_date"] >= mid)):
                    q = s[m]
                    r[f"{half}_roi"] = float(q["ret"].sum() / q["stake"].sum()) if len(q) else np.nan
                rois.append(r)
        cols = ["race_key", "race_date", "venue", "race_no", "grade", "manshu", "payout", "combo",
                "p_full", "p_no_chain", "p_chain_only", "c_p1_max", "c_p1_l1", "c_p1_entropy", "c_tri_entropy"]
        te[[c for c in cols if c in te.columns]].to_csv(out / f"race_level_{kind}.csv.gz", index=False,
                                                         encoding="utf-8")
        log(f"[MANSHU] {kind} 集計")

    pd.DataFrame(rows).to_csv(out / "picks.csv", index=False, encoding="utf-8")
    pd.DataFrame(stab).to_csv(out / "picks_by_month.csv", index=False, encoding="utf-8")
    pd.DataFrame(rois).to_csv(out / "roi.csv", index=False, encoding="utf-8")
    pd.concat(calib).to_csv(out / "calibration.csv", index=False, encoding="utf-8")
    # 展示前と展示後: 同じレースで印がどれだけ重なるか
    a = picks(preds_all["pre"][0].query("race_date >= @TEST_START"), "p_full", "day10")
    b = picks(preds_all["post"][0].query("race_date >= @TEST_START"), "p_full", "day10")
    res["day10_overlap_pre_post"] = len(set(a["race_key"]) & set(b["race_key"])) / max(len(a), 1)
    res["periods"] = {"train": [TRAIN_START, TEST_START], "test": [TEST_START, args.test_end],
                      "oof_folds": OOF_FOLDS}
    (out / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))


def main_tickets(kind, args, te, labs):
    """本番と同じ手順の買い目（2,000円/レース）。新モデル（本番と同じ学習済み）の final から作る。"""
    import ml_model as mm
    from ml_experiment import bet_all, finals_by_race
    from ml_pre_exhibition import races_for_bet

    cache = Path(args.cache) / f"main_tickets_{kind}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    table = pd.read_pickle(Path(args.cache) / f"{kind}.pkl")
    table = table[table["race_key"].isin(set(te["race_key"]))]
    model = mm.ChainModel.load(args.post_model if kind == "post" else args.pre_model)
    finals, _ = finals_by_race(model, table)
    preds = bet_all(finals, races_for_bet(table))
    pay = labs.set_index("race_key")
    rows = []
    for rk, r in preds.items():
        if rk not in pay.index:
            continue
        a, po = pay.at[rk, "combo"], float(pay.at[rk, "payout"])
        stake = sum(float(t.get("stake") or 0) for t in r["tickets"])
        hit = sum(float(t.get("stake") or 0) for t in r["tickets"] if t["combo"] == a)
        rows.append({"race_key": rk, "stake": stake, "ret": hit * po / 100.0})
    g = pd.DataFrame(rows).set_index("race_key")
    g.to_pickle(cache)
    return g


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("rates")
    a.add_argument("--out", required=True)
    a.add_argument("--start", default="")
    a.add_argument("--end", default="")
    e = sub.add_parser("eval")
    e.add_argument("--out", required=True)
    e.add_argument("--cache", required=True, help="特徴量の表（post.pkl・pre.pkl）と途中結果の置き場所")
    e.add_argument("--post-model", required=True, help="今の新モデル（reports/ml_v2 の 2y と同じ学習期間）")
    e.add_argument("--pre-model", required=True, help="展示前モデル（reports/pre_exhibition と同じ）")
    e.add_argument("--test-end", default="20261014")
    args = ap.parse_args()
    {"rates": cmd_rates, "eval": cmd_eval}[args.cmd](args)


if __name__ == "__main__":
    main()
