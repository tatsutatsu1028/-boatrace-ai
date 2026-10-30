"""
モデルの3連単の確率とオッズのずれ（期待値＝確率×オッズ）を調べる。新しい学習はしない。本番は変えない。

  - モデル: tools/ml_tune.py で作った今の本番と同じ設定のモデル（テスト期間を含まない）
  - オッズは2通り
      final:   最終オッズ（tools/odds_backfill.py が取った data/odds3t/。テスト期間 TEST_START〜TEST_END のレース）
      tracked: オッズ追跡（track_odds.py）が締切の約15分前に記録したオッズ（data/odds3t_tracked/。--tracked-start 以降）
  - 3連単の全組み合わせで 期待値 = モデルの確率 × オッズ を出し、期待値の区分ごとに
    その区分の組を全部100円ずつ買った場合の回収率・的中率と、モデルの確率・市場の確率（0.75/オッズ）と実際の的中率を比べる
  - 払戻は k_payouts の3連単の実績（tracked で買っても、払戻は最終オッズで決まる）
  - 内訳: オッズ帯・1着が1号艇かどうか。区間はレースを選び直す1,000回のブートストラップの95%

結果は公式データを含むので、非公開のデータ用リポジトリ（--out）にだけ書く。
標準出力には件数・時間など中身を含まない進み具合だけを出す。

  python tools/ml_edge_odds.py --out <データ用リポジトリ>/reports/ml_edge_odds \
      --model <データ用リポジトリ>/reports/ml_tune/models/ml-chain-v2-lgb-2y-current.joblib
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
import ml_experiment as ex  # noqa: E402
import ml_features as mf  # noqa: E402
import ml_model as mm  # noqa: E402
from data_paths import data_path  # noqa: E402

log = ex.log
EV_BINS = [0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, np.inf]
EV_LABELS = ["<0.5", "0.5-0.8", "0.8-1.0", "1.0-1.2", "1.2-1.5", "1.5-2.0", "2.0+"]
ODDS_BINS = [0, 10, 50, 100, np.inf]
ODDS_LABELS = ["<10", "10-50", "50-100", "100+"]
TAKE = 0.75  # 3連単の払戻率
SMALL_HITS = 30  # 的中がこれ未満の区分は回収率が当てにならない


def load_final(start, end):
    root = data_path("data/odds3t")
    o = pd.concat([pd.read_csv(p, dtype={"race_key": str, "combo": str}) for p in sorted(root.glob("odds3t_*.csv"))])
    o = o[(o["race_key"].str[:8] >= start) & (o["race_key"].str[:8] <= end)]
    t = pd.read_csv(data_path("reports/manshu/odds_targets.csv"), dtype=str)  # どのレースを取ったか（選び方の偏りの確認用）
    o["set"] = o["race_key"].map(t.drop_duplicates("race_key").set_index("race_key")["set"]).fillna("")
    return o


def load_tracked(start, end):
    o = pd.read_csv(data_path("data/odds3t_tracked/snapshots_202610.csv"), dtype={"race_key": str, "combo": str})
    o = o[(o["race_key"].str[:8] >= start) & (o["race_key"].str[:8] <= end)]
    return o.assign(set="tracked")


def combo_table(model, f, odds, start, end):
    """1組1行: モデルの確率・オッズ・期待値・実際に当たったか・払戻。"""
    races = set(odds["race_key"])
    t = f[(f["race_date"] >= start) & (f["race_date"] <= end) & f["race_key"].isin(races)]
    actual = ex.actual_trifecta(t)
    t = t[t["race_key"].isin(actual)]
    tri = mm.trifecta_table(*model.predict_tables(t))
    p = store.read_kind("k_payouts", start, end)
    p = p[p["bet_type"].astype(str).str.contains("3連単")]
    pay = {(str(r), str(c).strip()): float(v) for r, c, v in
           zip(p["race_key"], p["combo"], pd.to_numeric(p["payout"], errors="coerce"))}
    x = tri.merge(odds[["race_key", "combo", "odds", "set"]], on=["race_key", "combo"])
    x = x[x["odds"] > 0]
    # 全120通りのオッズがそろうレースだけ
    n = x.groupby("race_key")["combo"].transform("size")
    x = x[n == 120].copy()
    x["ev"] = x["prob"] * x["odds"]
    x["mkt"] = TAKE / x["odds"]
    x["mkt_norm"] = (1 / x["odds"]) / (1 / x["odds"]).groupby(x["race_key"]).transform("sum")
    x["hit"] = x["combo"] == x["race_key"].map(actual)
    x["ret"] = np.where(x["hit"], [pay.get(k, np.nan) for k in zip(x["race_key"], x["combo"])], 0.0)
    x["ret"] = x["ret"].fillna(0.0)
    x["ev_bin"] = pd.cut(x["ev"], EV_BINS, labels=EV_LABELS, right=False)
    x["odds_bin"] = pd.cut(x["odds"], ODDS_BINS, labels=ODDS_LABELS, right=False)
    x["first"] = np.where(x["combo"].str[0] == "1", "1着が1号艇", "1着が1号艇以外")
    return x


def summarize(x, n_boot=1000, seed=0):
    """区分ごとの集計。区間はレースを選び直す（同じレースの組はまとめて選ぶ）。"""
    races = pd.unique(x["race_key"])
    pos = pd.Series(np.arange(len(races)), index=races)
    r = pos[x["race_key"]].to_numpy()
    cols = {"stake": np.full(len(x), 100.0), "ret": x["ret"].to_numpy(float), "hit": x["hit"].to_numpy(float),
            "prob": x["prob"].to_numpy(float), "mkt": x["mkt_norm"].to_numpy(float), "n": np.ones(len(x))}
    per = {k: np.bincount(r, weights=v, minlength=len(races)) for k, v in cols.items()}
    tot = {k: v.sum() for k, v in per.items()}
    idx = np.random.default_rng(seed).integers(0, len(races), (n_boot, len(races)))
    bs = {k: v[idx].sum(axis=1) for k, v in per.items()}

    def ci(a):
        return float(np.nanquantile(a, 0.025)), float(np.nanquantile(a, 0.975))

    with np.errstate(invalid="ignore", divide="ignore"):
        out = {"races": len(races), "combos": int(tot["n"]), "hits": int(tot["hit"]),
               "roi": tot["ret"] / tot["stake"], "hit_rate": tot["hit"] / tot["n"],
               "model_prob": tot["prob"] / tot["n"], "market_prob": tot["mkt"] / tot["n"],
               "mean_odds": float(x["odds"].mean()), "mean_ev": float(x["ev"].mean())}
        out["roi_lo"], out["roi_hi"] = ci(bs["ret"] / bs["stake"])
        out["hit_lo"], out["hit_hi"] = ci(bs["hit"] / bs["n"])
        out["model_minus_actual"] = out["model_prob"] - out["hit_rate"]
        out["mma_lo"], out["mma_hi"] = ci((bs["prob"] - bs["hit"]) / bs["n"])
        out["actual_over_model"] = out["hit_rate"] / out["model_prob"]
        out["aom_lo"], out["aom_hi"] = ci(bs["hit"] / bs["prob"])
    out["few_hits"] = out["hits"] < SMALL_HITS
    return out


def tables(x, name, n_boot):
    rows = []
    for ev, d in x.groupby("ev_bin", observed=True):
        rows.append({"odds": name, "breakdown": "期待値", "ev_bin": ev, "sub": "", **summarize(d, n_boot)})
    for col, kind in (("odds_bin", "期待値×オッズ帯"), ("first", "期待値×1着")):
        for (ev, s), d in x.groupby(["ev_bin", col], observed=True):
            rows.append({"odds": name, "breakdown": kind, "ev_bin": ev, "sub": s, **summarize(d, n_boot)})
    for col, kind in (("odds_bin", "オッズ帯"), ("first", "1着")):
        for s, d in x.groupby(col, observed=True):
            rows.append({"odds": name, "breakdown": kind, "ev_bin": "全部", "sub": s, **summarize(d, n_boot)})
    for s, d in x.groupby("set"):
        for ev, dd in d.groupby("ev_bin", observed=True):
            rows.append({"odds": name, "breakdown": "取得の組（レースの選び方）", "ev_bin": ev, "sub": s,
                         **summarize(dd, n_boot)})
    # 上側の区分をまとめたもの（「期待値がこれ以上なら買う」の形）
    for th in (1.0, 1.2, 1.5, 2.0):
        d = x[x["ev"] >= th]
        rows.append({"odds": name, "breakdown": "期待値以上", "ev_bin": f">={th}", "sub": "",
                     **summarize(d, n_boot)})
    return pd.DataFrame(rows)


def minutes_before_deadline(odds, start, end):
    """追跡したオッズを記録した時刻が、番組表の締切予定の何分前か（分位点）。"""
    b = store.read_kind("b_programs", start, end).drop_duplicates("race_key")
    dl = pd.to_datetime(b["race_date"].astype(str) + " " + b["deadline"].astype(str), format="%Y%m%d %H:%M",
                        errors="coerce")
    dl = pd.Series(dl.to_numpy(), index=b["race_key"].astype(str))
    o = odds.drop_duplicates("race_key")
    m = (o["race_key"].map(dl) - pd.to_datetime(o["fetched_at"])).dt.total_seconds() / 60
    q = m.dropna().quantile([0, 0.1, 0.5, 0.9, 1])
    return {"races": int(m.notna().sum()), **{str(k): float(v) for k, v in q.items()}}


def drift(x):
    """追跡したオッズ（締切の約15分前）と、的中した組の最終オッズ（払戻/100）の比。"""
    h = x[x["hit"] & (x["ret"] > 0)]
    r = (h["ret"] / 100.0) / h["odds"]
    q = r.quantile([0.1, 0.25, 0.5, 0.75, 0.9])
    by = h.assign(r=r).groupby("odds_bin", observed=True)["r"].median()
    return {"hits": int(len(h)), "final_over_tracked_quantiles": {str(k): float(v) for k, v in q.items()},
            "median_by_tracked_odds": {str(k): float(v) for k, v in by.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--features", default="")
    ap.add_argument("--tracked-start", default="20261023")
    ap.add_argument("--tracked-end", default="20261028")
    ap.add_argument("--boot", type=int, default=1000)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    model = mm.ChainModel.load(args.model)
    if str(model.info.get("train_to", "")) >= ex.CALIB_START or str(model.info.get("calib_to", "")) >= ex.TEST_START:
        raise SystemExit("モデルの学習・調整の期間がテスト期間と重なっている")
    f = pd.read_pickle(args.features) if args.features and Path(args.features).exists() \
        else mf.build_table(*mf.load_sources())

    meta = {"model": model.version, "ev_bins": EV_LABELS, "odds_bins": ODDS_LABELS, "takeout_rate": TAKE,
            "few_hits": SMALL_HITS, "bootstrap": args.boot}
    res = []
    for name, odds, s, e in (
        ("final", load_final(ex.TEST_START, ex.TEST_END), ex.TEST_START, ex.TEST_END),
        ("tracked", load_tracked(args.tracked_start, args.tracked_end), args.tracked_start, args.tracked_end),
    ):
        x = combo_table(model, f, odds, s, e)
        x.to_csv(out / f"combo_level_{name}.csv.gz", index=False, encoding="utf-8")
        meta[name] = {"period": [s, e], "races": int(x["race_key"].nunique()),
                      "sets": x.drop_duplicates("race_key")["set"].value_counts().to_dict()}
        if name == "tracked":
            meta["tracked_drift"] = drift(x)
            meta[name]["minutes_before_deadline"] = minutes_before_deadline(odds, s, e)
        res.append(tables(x, name, args.boot))
        log(f"[EDGE] {name} {x['race_key'].nunique()}レース {len(x)}組 {time.time() - t0:.0f}秒")
    pd.concat(res, ignore_index=True).to_csv(out / "summary.csv", index=False, encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float))
    log(f"[EDGE] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
