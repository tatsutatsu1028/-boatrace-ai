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
# 万舟の定義・特徴量は本番（manshu.py）と同じものを使う
from manshu import MANSHU_YEN, chain_summary, race_features  # noqa: E402,F401


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


# ---------------------------------------------------------------
# オッズを使う万舟狙いの買い目（odds）
# ---------------------------------------------------------------
ODDS_MIN = 100.0   # 「オッズ100倍以上」
ODDS_N = 10        # 1レースの点数（各100円）


def load_odds(data_dir):
    """data/odds3t/ の最終オッズ（race_key, combo, odds）。"""
    files = sorted(Path(data_dir).glob("odds3t_*.csv"))
    if not files:
        raise SystemExit("data/odds3t にオッズがありません（Odds Backfill を先に動かす）")
    o = pd.concat([pd.read_csv(f, dtype={"race_key": str, "combo": str}) for f in files], ignore_index=True)
    o["odds"] = pd.to_numeric(o["odds"], errors="coerce")
    return o.drop_duplicates(["race_key", "combo"], keep="last")


def is_outer_top2(combo):
    """4〜6号艇が1着か2着に入る組か。"""
    s = pd.Series(combo).astype(str)
    return s.str[0].isin(list("456")) | s.str[2].isin(list("456"))


POINTS = (5, 10, 15)
ODDS_BANDS = [100, 200, 500, 1000, 1e9]


def odds_strategies(tri, odds, n=ODDS_N, odds_min=ODDS_MIN, claude=None):
    """tri: race_key, combo, prob（モデルの3連単確率）/ odds: race_key, combo, odds
    claude: race_key, rank, combo（Claude が選んだ順。tools/manshu_claude_aim.py の結果）
    → {名前: race_key, combo の表}（1レース n 点まで）"""
    t = tri.merge(odds, on=["race_key", "combo"], how="inner")
    t["outer"] = is_outer_top2(t["combo"]).to_numpy()
    t["ev"] = t["prob"] * t["odds"]
    hi = t[t["odds"] >= odds_min]
    out = {}
    out["A 確率順"] = hi.sort_values(["race_key", "prob"], ascending=[True, False]).groupby("race_key").head(n)
    b = hi.sort_values(["race_key", "outer", "prob"], ascending=[True, False, False])
    out["B 4〜6号艇が1〜2着の組を優先"] = b.groupby("race_key").head(n)
    out["C 期待値順（確率×オッズ）"] = hi.sort_values(["race_key", "ev"], ascending=[True, False]).groupby("race_key").head(n)
    if claude is not None and len(claude):
        c = claude.sort_values(["race_key", "rank"])
        out["D Claude"] = c.groupby("race_key").head(n)[["race_key", "combo"]]
    r = t[t["combo"].str[0] != "1"].sort_values(["race_key", "prob"], ascending=[True, False])
    out["参考 1号艇を1着から外した上位（オッズなし）"] = r.groupby("race_key").head(n)
    return out


def calibration_longshots(tri, odds, lab, kind):
    """モデルの人気薄（オッズ100倍以上）の組の確率の当てはまり。オッズの帯・モデルの確率の帯ごとに
    「モデルの確率の合計」「実際に来た数」「オッズから見た確率（1/オッズをレース内で合計1にしたもの）の合計」。"""
    t = tri.merge(odds, on=["race_key", "combo"], how="inner")
    inv = 1.0 / t["odds"]
    t["market"] = inv / inv.groupby(t["race_key"]).transform("sum")
    act = lab.set_index("race_key")["combo"]
    t = t[t["race_key"].isin(act.index)]
    t["hit"] = (t["combo"] == t["race_key"].map(act)).astype(float)
    hi = t[t["odds"] >= ODDS_MIN].copy()
    rows = []
    hi["odds_band"] = pd.cut(hi["odds"], ODDS_BANDS, right=False).astype(str)
    hi["prob_band"] = pd.cut(hi["prob"], [0, 0.002, 0.005, 0.01, 0.02, 0.05, 1.0], right=False).astype(str)
    for by in ("odds_band", "prob_band"):
        g = hi.groupby(by).agg(combos=("hit", "size"), model_prob=("prob", "sum"), actual=("hit", "sum"),
                               market_prob=("market", "sum"), avg_odds=("odds", "mean"))
        g["actual_per_model"] = g["actual"] / g["model_prob"]
        g["actual_per_market"] = g["actual"] / g["market_prob"]
        rows.append(g.reset_index().rename(columns={by: "band"}).assign(by=by, version=kind))
    tot = {"version": kind, "by": "all", "band": "100倍以上の全部", "combos": len(hi),
           "model_prob": hi["prob"].sum(), "actual": hi["hit"].sum(), "market_prob": hi["market"].sum(),
           "avg_odds": hi["odds"].mean(), "races": hi["race_key"].nunique()}
    tot["actual_per_model"] = tot["actual"] / tot["model_prob"]
    tot["actual_per_market"] = tot["actual"] / tot["market_prob"]
    rows.append(pd.DataFrame([tot]))
    return pd.concat(rows, ignore_index=True)


def _roi_row(sel, lab, keys, mid, **meta):
    g = ticket_returns(sel[sel["race_key"].isin(keys)], lab)
    g = g[g["stake"] > 0].join(lab.set_index("race_key")[["race_date", "manshu"]])
    if not len(g):
        return None
    hit = g["ret"] > 0
    r = dict(meta, races_bet=len(g), points_per_race=float(g["stake"].mean() / 100),
             stake=float(g["stake"].sum()), ret=float(g["ret"].sum()),
             roi=float(g["ret"].sum() / g["stake"].sum()), hit_rate=float(hit.mean()),
             avg_hit_return=float(g.loc[hit, "ret"].mean()) if hit.any() else np.nan,
             manshu_rate=float(g["manshu"].mean()))
    r["roi_ci95"] = boot_ci(g.reset_index(), lambda q: q["ret"].sum() / q["stake"].sum())
    for half, m in (("first_half", g["race_date"] < mid), ("second_half", g["race_date"] >= mid)):
        q = g[m]
        r[f"{half}_roi"] = float(q["ret"].sum() / q["stake"].sum()) if len(q) else np.nan
        r[f"{half}_races"] = int(len(q))
    return r


def load_claude(out):
    f = out / "claude_aim.jsonl"
    if not f.exists():
        return None
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("status") != "ok":
            continue
        for i, c in enumerate(d.get("tickets") or []):
            rows.append({"race_key": d["race_key"], "version": d.get("version", "post"), "rank": i, "combo": c})
    return pd.DataFrame(rows)


def cmd_odds(args):
    out = Path(args.out)
    cache = Path(args.cache)
    from data_paths import data_path

    odds = load_odds(args.odds_dir or data_path("data/odds3t"))
    have = set(odds["race_key"])
    targets = pd.read_csv(out / "odds_targets.csv", dtype=str)
    lab = labeled_races(TEST_START, args.test_end)
    lab = lab[lab["race_key"].isin(have)]
    mid = sorted(lab["race_date"].unique())[lab["race_date"].nunique() // 2]
    claude = load_claude(out)
    rows, cal = [], []
    for kind in ("pre", "post"):
        full = pd.read_csv(out / f"race_level_{kind}.csv.gz", dtype={"race_date": str})
        sets = {"印: 毎日10レース": picks(full, "p_full", "day10"), "印: 毎日20レース": picks(full, "p_full", "day20")}
        rand = set(targets.loc[targets["set"] == "random", "race_key"])
        sets["比較: 印の無いレース（無作為800）"] = full[full["race_key"].isin(rand)]
        if kind == "post":
            sets.pop("印: 毎日20レース")  # 展示後版の20レースは取っていない
        cl = claude[claude["version"] == kind] if claude is not None else None
        if cl is not None and len(cl):
            sets["Claude に渡したレース（同じレースで比べる）"] = full[full["race_key"].isin(set(cl["race_key"]))]
        ch = pd.read_pickle(cache / f"chain_{kind}.pkl")
        tri = ch["tri"][ch["tri"]["race_key"].isin(have)]
        for n in POINTS:
            strategies = odds_strategies(tri, odds, n=n, claude=cl)
            for sname, d in sets.items():
                keys = set(d["race_key"]) & have
                for tname, sel in strategies.items():
                    r = _roi_row(sel, lab, keys, mid, version=kind, races_set=sname, tickets=tname, points=n,
                                 races_in_set=len(d), races_with_odds=len(keys))
                    if r:
                        rows.append(r)
        cal.append(calibration_longshots(tri, odds, lab, kind))
        log(f"[MANSHU] odds {kind}")
    pd.DataFrame(rows).to_csv(out / "odds_roi.csv", index=False, encoding="utf-8")
    pd.concat(cal, ignore_index=True).to_csv(out / "odds_calibration.csv", index=False, encoding="utf-8")
    log(f"[MANSHU] odds 終了 {len(have)}レース分のオッズ")

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
    o = sub.add_parser("odds", help="オッズ100倍以上から選ぶ万舟狙いの買い目（Odds Backfill のオッズを使う）")
    o.add_argument("--out", required=True, help="reports/manshu（race_level_*.csv.gz・odds_targets.csv がある）")
    o.add_argument("--cache", required=True, help="eval の途中結果（chain_pre.pkl・chain_post.pkl）")
    o.add_argument("--odds-dir", default="", help="既定: <BOATRACE_DATA_DIR>/data/odds3t")
    o.add_argument("--test-end", default="20261014")
    args = ap.parse_args()
    {"rates": cmd_rates, "eval": cmd_eval, "odds": cmd_odds}[args.cmd](args)


if __name__ == "__main__":
    main()
