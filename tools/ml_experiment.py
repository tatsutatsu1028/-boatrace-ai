"""
新しい予想モデル（ml_model.ChainModel）の検証。

  - 学習期間・確率の調整期間・テスト期間を日付で分ける
  - 「1年分を全項目で学習」（出走表・直前情報がそろうレースだけ）と
    「2年分を使い、出走表・直前情報の無い期間はその項目を欠損として学習」の2通りを比べる
  - テスト期間の同じレースで今の本番モデル（tools/ml_compare_old.py の結果）と比べる
    （1着的中率・候補内的中率・賭けた買い目での的中率・回収率・Brier・LogLoss、
      1号艇が負けたレース・2着3着に外枠が入ったレースの内訳、表示確率と実際の的中率）
  - 特徴量の重要度

結果の表・レース別のデータは公式データを含むので、非公開のデータ用リポジトリ（--out）にだけ書く。
標準出力には件数・時間など中身を含まない進み具合だけを出す。

  python tools/ml_experiment.py --out <データ用リポジトリ>/reports/ml_v1
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import history_store as store  # noqa: E402
import ml_features as mf  # noqa: E402
import ml_model as mm  # noqa: E402

TEST_START, TEST_END = "20260707", "20261006"
CALIB_START = "20260607"
BINS = [0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0001]
HIT_BINS = [0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0001]


def log(*a):
    print(*a, flush=True)


def full_races(f):
    """6艇とも出走表・直前情報がそろっているレースだけ。"""
    ok = f.groupby("race_key")["has_pages"].transform("all")
    return f[ok]


def split(f, variant):
    calib = f[(f["race_date"] >= CALIB_START) & (f["race_date"] < TEST_START)]
    train = f[f["race_date"] < CALIB_START]
    if variant == "1y":
        train = full_races(train)
        calib = full_races(calib)
    test = f[(f["race_date"] >= TEST_START) & (f["race_date"] <= TEST_END)]
    return train, calib, test


# ---------------------------------------------------------------
# 確率の正確さ
# ---------------------------------------------------------------
def first_metrics(first, test):
    y = test[["race_key", "lane", "finish"]]
    x = first.merge(y, on=["race_key", "lane"])
    x["y"] = (x["finish"] == 1).astype(float)
    races = x.groupby("race_key")["y"].transform("sum") == 1
    x = x[races]
    fav = x.loc[x.groupby("race_key")["p_first"].idxmax()]
    ll = -np.log(np.clip(x.loc[x["y"] == 1, "p_first"], 1e-12, None)).mean()
    brier = ((x["p_first"] - x["y"]) ** 2).groupby(x["race_key"]).sum().mean()
    return {"races": int(x["race_key"].nunique()), "first_hit": float(fav["y"].mean()),
            "first_logloss": float(ll), "first_brier": float(brier)}, x


def calib_table(p, y, bins=BINS):
    b = pd.cut(p, bins, right=False)
    t = pd.DataFrame({"p": p, "y": y, "b": b}).groupby("b", observed=True).agg(
        n=("y", "size"), shown=("p", "mean"), actual=("y", "mean"))
    t["gap"] = t["actual"] - t["shown"]
    return t.reset_index().assign(b=lambda d: d["b"].astype(str))


# ---------------------------------------------------------------
# 買い目（本番と同じ手順）
# ---------------------------------------------------------------
def finals_by_race(model, test):
    """テスト期間の全レースの final（prediction.predict と同じ形）を作る（まとめて横持ちにしてから切り出す）。"""
    first, second, third = model.predict_tables(test)
    w2 = second.pivot_table(index=["race_key", "w_lane"], columns="lane", values="p")
    w3 = third.pivot_table(index=["race_key", "w_lane", "s_lane"], columns="lane", values="p")
    p1_all = first.pivot_table(index="race_key", columns="lane", values="p_first")
    w2_g = {rk: g.droplevel(0) for rk, g in w2.groupby(level=0)}
    w3_g = {rk: g.droplevel(0) for rk, g in w3.groupby(level=0)}
    out = {}
    for rk, row in p1_all.iterrows():
        lanes = [int(c) for c in row.index if np.isfinite(row[c])]
        p1 = {ln: float(row[ln]) for ln in lanes}
        cols = {"lane": lanes, "p_first": [p1[ln] for ln in lanes]}
        p2m = dict.fromkeys(lanes, 0.0)
        p3m = dict.fromkeys(lanes, 0.0)
        s2 = w2_g[rk]
        for a in lanes:
            cond = s2.loc[a]
            cols[f"p_second_given_{a}"] = [0.0 if ln == a else float(np.nan_to_num(cond.get(ln, 0.0))) for ln in lanes]
            for ln in lanes:
                if ln != a:
                    p2m[ln] += p1[a] * float(np.nan_to_num(cond.get(ln, 0.0)))
        s3 = w3_g[rk]
        for (a, b_), cond in s3.iterrows():
            a, b_ = int(a), int(b_)
            cols[f"p_third_given_{a}_{b_}"] = [0.0 if ln in (a, b_) else float(np.nan_to_num(cond.get(ln, 0.0)))
                                              for ln in lanes]
            pab = p1[a] * float(np.nan_to_num(s2.loc[a].get(b_, 0.0)))
            for ln in lanes:
                if ln not in (a, b_):
                    p3m[ln] += pab * float(np.nan_to_num(cond.get(ln, 0.0)))
        cols["p_second"] = [p2m[ln] for ln in lanes]
        cols["p_third"] = [p3m[ln] for ln in lanes]
        fin = pd.DataFrame(cols)
        fin["model_version"] = model.version
        fin["adjustment"] = 0.0
        out[rk] = fin
    return out, first


_BET_RACES = {}


def _bet_one(item):
    from ml_compare_old import bet

    rk, fin = item
    try:
        r = bet(fin, _BET_RACES[rk])
    except Exception as e:  # noqa: BLE001
        return rk, f"{type(e).__name__}: {e}"
    r["p_first"] = dict(zip(fin["lane"].astype(int), fin["p_first"].astype(float)))
    return rk, r


def bet_all(finals, races, workers=4):
    """本番と同じ買い目・資金配分を、レースごとに並列で作る。"""
    import multiprocessing as mp

    _BET_RACES.clear()
    _BET_RACES.update(races)
    with mp.get_context("fork").Pool(workers) as pool:
        res = pool.map(_bet_one, list(finals.items()), chunksize=50)
    errors = [v for _, v in res if isinstance(v, str)]
    if errors:
        log(f"[EXP] bet error {len(errors)}件 例: {errors[0]}")
    return {rk: v for rk, v in res if not isinstance(v, str)}


def load_old(old_dir):
    pred, race = {}, {}
    for p in sorted(Path(old_dir).glob("old_*.pkl")):
        with open(p, "rb") as fh:
            d = pickle.load(fh)
        pred.update(d["pred"])
        race.update(d["race"])
    return pred, race


def payouts():
    p = store.read_kind("k_payouts", TEST_START, TEST_END)
    p = p[p["bet_type"].astype(str).str.contains("3連単")]
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    p["combo"] = p["combo"].astype(str).str.strip()
    return p.groupby("race_key").apply(lambda g: dict(zip(g["combo"], g["payout"])), include_groups=False).to_dict()


def actual_trifecta(test):
    t = test[test["finish"].isin([1, 2, 3])].sort_values("finish")
    g = t.groupby("race_key")["lane"].apply(lambda s: "-".join(str(int(v)) for v in s) if len(s) == 3 else None)
    return g.dropna().to_dict()


def ticket_rows(name, preds, actual, pay):
    """1レース1行: 候補内的中・賭けた買い目での的中・払戻・表示した的中確率。"""
    rows = []
    for rk, r in preds.items():
        a = actual.get(rk)
        if a is None:
            continue
        tk = r["tickets"]
        combos = [t["combo"] for t in tk]
        stake = sum(float(t.get("stake") or 0) for t in tk)
        bet_combos = [t["combo"] for t in tk if float(t.get("stake") or 0) > 0]
        hit_stake = sum(float(t.get("stake") or 0) for t in tk if t["combo"] == a)
        po = pay.get(rk, {}).get(a, np.nan)
        rows.append({
            "model": name, "race_key": rk, "actual": a,
            "winner": int(a[0]), "second": int(a[2]), "third": int(a[4]),
            "candidate_hit": a in combos, "bet_hit": a in bet_combos,
            "n_tickets": len(combos), "stake": stake,
            "return": hit_stake * po / 100.0 if np.isfinite(po) else 0.0,
            "payout": po,
            "hit_probability": r["hit_probability"],
            "p_actual": r["tri"].get(a, np.nan),
            "favorite": max(r["p_first"], key=r["p_first"].get),
            "p_favorite": max(r["p_first"].values()),
        })
    return pd.DataFrame(rows)


def summarize(df):
    def agg(d):
        return pd.Series({
            "races": len(d),
            "first_hit": (d["favorite"] == d["winner"]).mean(),
            "candidate_hit": d["candidate_hit"].mean(),
            "bet_hit": d["bet_hit"].mean(),
            "avg_tickets": d["n_tickets"].mean(),
            "stake": d["stake"].sum(),
            "return": d["return"].sum(),
            "roi": d["return"].sum() / d["stake"].sum() if d["stake"].sum() else np.nan,
            "shown_hit_probability": d["hit_probability"].mean(),
            "trifecta_logloss": -np.log(np.clip(d["p_actual"].fillna(1e-6), 1e-6, None)).mean(),
        })

    segs = {
        "全レース": df,
        "1号艇が負けたレース": df[df["winner"] != 1],
        "1号艇が勝ったレース": df[df["winner"] == 1],
        "2着か3着に外枠(4-6)": df[(df["second"] >= 4) | (df["third"] >= 4)],
        "2着・3着とも内枠(1-3)": df[(df["second"] <= 3) & (df["third"] <= 3)],
    }
    out = []
    for seg, d in segs.items():
        for m, dm in d.groupby("model"):
            out.append(agg(dm).rename(f"{seg}|{m}"))
    t = pd.DataFrame(out)
    t.index = pd.MultiIndex.from_tuples([tuple(i.split("|")) for i in t.index], names=["segment", "model"])
    return t.reset_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--features", default="", help="ml_features.build_table の結果（pickle）")
    ap.add_argument("--old", default="", help="tools/ml_compare_old.py の出力フォルダ")
    ap.add_argument("--variants", default="1y,2y")
    ap.add_argument("--importance", action="store_true")
    ap.add_argument("--engine", default="lgb", choices=["lgb", "hgb"])
    ap.add_argument("--reuse", action="store_true", help="保存済みのモデルがあれば学習し直さない")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    if args.features and Path(args.features).exists():
        f = pd.read_pickle(args.features)
    else:
        f = mf.build_table(*mf.load_sources())
    log(f"[EXP] 特徴量 {f.shape} {time.time() - t0:.0f}秒")

    old_pred, old_race = load_old(args.old) if args.old else ({}, {})
    pay = payouts()
    test_all = f[(f["race_date"] >= TEST_START) & (f["race_date"] <= TEST_END)]
    actual = actual_trifecta(test_all)

    summary = {"periods": {"calib": [CALIB_START, TEST_START], "test": [TEST_START, TEST_END]}, "variants": {}}
    tickets_all = []
    calib_rows = []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ml_compare_old import bet  # noqa: E402

    for variant in args.variants.split(","):
        train, calib, test = split(f, variant)
        version = f"{mm.MODEL_FAMILY}-v1-{args.engine}-{variant}"
        mpath = out / "models" / f"{version}.joblib"
        if args.reuse and mpath.exists():
            model = mm.ChainModel.load(mpath)
        else:
            model = mm.ChainModel(mf.FEATURES, version, engine=args.engine)
            model.fit(train, calib, log=log)
            model.save(mpath)

        fm, firsts = first_metrics(model.predict_tables(test)[0], test)
        info = dict(model.info, temps=model.temps, **fm)

        # 1着確率の表示と実際
        ct = calib_table(firsts["p_first"].to_numpy(), firsts["y"].to_numpy())
        calib_rows.append(ct.assign(model=version, kind="1着確率"))

        # 買い目（今のモデルと同じレース）
        if old_pred:
            keys = [rk for rk in old_pred if rk in set(test["race_key"])]
            tsub = test[test["race_key"].isin(keys)]
            t1 = time.time()
            finals, _ = finals_by_race(model, tsub)
            log(f"[EXP] {version} final {len(finals)}レース {time.time() - t1:.0f}秒")
            preds = bet_all(finals, old_race)
            log(f"[EXP] {version} 買い目 {len(preds)}レース {time.time() - t1:.0f}秒")
            tickets_all.append(ticket_rows(version, preds, actual, pay))
        summary["variants"][variant] = info
        log(f"[EXP] {version} 完了 {time.time() - t0:.0f}秒")

        if args.importance:
            imp = pd.concat([model.importance(test, stage=s).rename(f"stage{s}") for s in (1, 2, 3)], axis=1)
            group_of = {c: g for g, cols in mf.FEATURE_GROUPS.items() for c in cols}
            imp["group"] = [group_of.get(c, "上位艇との関係" if c[:2] in ("w_", "s_") else "") for c in imp.index]
            imp.to_csv(out / f"importance_{variant}.csv", encoding="utf-8")
            log(f"[EXP] {version} 重要度 {time.time() - t0:.0f}秒")

    if old_pred:
        old_keys = set(old_pred)
        tickets_all.append(ticket_rows("current", old_pred, actual, pay))
        # 今のモデルの1着確率の正確さ（同じレース）
        rows = [{"race_key": rk, "lane": ln, "p_first": p} for rk, r in old_pred.items()
                for ln, p in r["p_first"].items()]
        om, ofirst = first_metrics(pd.DataFrame(rows), test_all[test_all["race_key"].isin(old_keys)])
        summary["variants"]["current"] = om
        calib_rows.append(calib_table(ofirst["p_first"].to_numpy(), ofirst["y"].to_numpy())
                          .assign(model="current", kind="1着確率"))
        tk = pd.concat(tickets_all, ignore_index=True)
        common = set.intersection(*[set(d["race_key"]) for _, d in tk.groupby("model")])
        tk = tk[tk["race_key"].isin(common)]
        tk.to_csv(out / "race_level.csv.gz", index=False, encoding="utf-8")
        summarize(tk).to_csv(out / "summary_tickets.csv", index=False, encoding="utf-8")
        for m, d in tk.groupby("model"):
            calib_rows.append(calib_table(d["hit_probability"].to_numpy(), d["candidate_hit"].astype(float).to_numpy(),
                                          HIT_BINS).assign(model=m, kind="買い目全体の的中確率"))
    pd.concat(calib_rows, ignore_index=True).to_csv(out / "calibration.csv", index=False, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    log(f"[EXP] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
