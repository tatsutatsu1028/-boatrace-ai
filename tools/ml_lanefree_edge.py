"""
「艇番なしモデルの実力トップが1号艇以外」のレースに妙味があるかを調べる。新しい学習はしない。本番は変えない。

  - モデル: tools/ml_tune.py で作った今の本番と同じ設定のモデル（テスト期間を含まない）
  - 対象: テスト期間（tools/ml_experiment.py と同じ）で、艇番なしモデルの出力（la_p）が最大の艇が1号艇以外のレース
  - 当たり方: その艇の実際の1着率・2連対率・3連対率と、モデルの最終の確率（1着・2着以内・3着以内）を比べる
  - 回収率（払戻は k_payouts）: その艇の単勝、その艇1着固定の2連単・3連単（相手はモデルの条件付き確率の上位）を
    1点100円で買った場合。比較に同じレースの本番の買い目（tools/ml_tune.py の race_level_tickets.csv.gz の current）
  - 内訳: 実力トップの艇番・実力差（1位と2位の la_p の差）・場・1号艇の実力順位
  - 回収率・的中率・（実際−モデル）の差に、レースを選び直す1,000回のブートストラップで95%区間を付ける

結果は公式データを含むので、非公開のデータ用リポジトリ（--out）にだけ書く。
標準出力には件数・時間など中身を含まない進み具合だけを出す。

  python tools/ml_lanefree_edge.py --out <データ用リポジトリ>/reports/ml_shap/lanefree_edge \
      --model <データ用リポジトリ>/reports/ml_tune/models/ml-chain-v2-lgb-2y-current.joblib \
      --tickets <データ用リポジトリ>/reports/ml_tune/race_level_tickets.csv.gz
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

log = ex.log
GAP_BINS = [0, 0.05, 0.10, 0.20, 1.01]
GAP_LABELS = ["<5pt", "5-10pt", "10-20pt", "20pt+"]
SMALL = 100  # これ未満のレース数の区分は「件数が少ない」
# 買い方: (名前, 券種, 点数)
BETS = [("単勝", "単勝", 1), ("2連単1点", "2連単", 1), ("2連単2点", "2連単", 2),
        ("3連単2点", "3連単", 2), ("3連単4点", "3連単", 4), ("3連単6点", "3連単", 6)]


def payout_table():
    p = store.read_kind("k_payouts", ex.TEST_START, ex.TEST_END)
    p = p[p["bet_type"].isin(["単勝", "2連単", "3連単"])].copy()
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    p["combo"] = p["combo"].astype(str).str.strip()
    p["race_key"] = p["race_key"].astype(str)
    return {k: dict(zip(g["combo"], g["payout"])) for k, g in p.groupby(["race_key", "bet_type"])}


def boat_table(model, test):
    """1艇1行: la_p（艇番なしの実力）・最終の確率（1着・2着以内・3着以内）・実際の着順。"""
    t = model.with_lane_free(test)
    first, second, third = model.predict_tables(t)
    tri = mm.trifecta_table(first, second, third)
    parts = tri["combo"].str.split("-", expand=True).astype(int)
    tri = tri.assign(a=parts[0], b=parts[1], c=parts[2])
    top2 = pd.concat([tri[["race_key", "a", "prob"]].rename(columns={"a": "lane"}),
                      tri[["race_key", "b", "prob"]].rename(columns={"b": "lane"})])
    top3 = pd.concat([top2, tri[["race_key", "c", "prob"]].rename(columns={"c": "lane"})])
    b = t[["race_key", "race_date", "jcd", "lane", "finish", "la_p"]].copy()
    b["lane"] = b["lane"].astype(int)
    b = b.merge(first.assign(lane=first["lane"].astype(int)), on=["race_key", "lane"])
    b = b.merge(top2.groupby(["race_key", "lane"])["prob"].sum().rename("p_top2").reset_index(), on=["race_key", "lane"])
    b = b.merge(top3.groupby(["race_key", "lane"])["prob"].sum().rename("p_top3").reset_index(), on=["race_key", "lane"])
    b["la_rank"] = b.groupby("race_key")["la_p"].rank(ascending=False, method="first").astype(int)
    b["p_rank"] = b.groupby("race_key")["p_first"].rank(ascending=False, method="first").astype(int)
    return b, tri


def race_table(b, tri, pay, actual):
    """1レース1行: 実力トップの艇と、その艇を軸にした買い目の払戻。"""
    b = b[b["race_key"].isin(actual)]
    top = b[b["la_rank"] == 1].set_index("race_key")
    sec = b[b["la_rank"] == 2].set_index("race_key")
    l1 = b[b["lane"] == 1].set_index("race_key")
    r = pd.DataFrame(index=top.index)
    r["race_date"], r["jcd"] = top["race_date"], top["jcd"].astype(str).str.zfill(2)
    r["top_lane"] = top["lane"]
    r["gap"] = top["la_p"] - sec["la_p"]
    r["gap_bin"] = pd.cut(r["gap"], GAP_BINS, labels=GAP_LABELS, right=False)
    r["lane1_la_rank"] = l1["la_rank"]
    r["top_la_p"] = top["la_p"]
    r["top_p_first"], r["top_p_top2"], r["top_p_top3"] = top["p_first"], top["p_top2"], top["p_top3"]
    r["top_p_rank"] = top["p_rank"]
    r["lane1_p_first"] = l1["p_first"]
    fin = top["finish"]
    r["top_win"], r["top_top2"], r["top_top3"] = (fin == 1), (fin <= 2), (fin <= 3)
    r["lane1_win"] = l1["finish"] == 1
    r["actual"] = r.index.map(actual)

    # 実力トップ1着固定の買い目（相手はモデルの条件付き確率の上位）
    tri = tri[tri["race_key"].isin(r.index)]
    fixed = tri.merge(r["top_lane"].rename("a").reset_index(), on=["race_key", "a"])
    fixed = fixed.sort_values(["race_key", "prob"], ascending=[True, False])
    ex2 = fixed.groupby(["race_key", "b"])["prob"].sum().reset_index().sort_values(
        ["race_key", "prob"], ascending=[True, False])
    pick2 = {rk: [f"{r.at[rk, 'top_lane']}-{v}" for v in g["b"]] for rk, g in ex2.groupby("race_key")}
    pick3 = {rk: g["combo"].tolist() for rk, g in fixed.groupby("race_key")}
    for name, kind, n in BETS:
        stake, ret = [], []
        for rk in r.index:
            if kind == "単勝":
                combos = [str(r.at[rk, "top_lane"])]
            elif kind == "2連単":
                combos = pick2.get(rk, [])[:n]
            else:
                combos = pick3.get(rk, [])[:n]
            po = pay.get((rk, kind), {})
            stake.append(100 * len(combos))
            ret.append(sum(float(po.get(c, 0) or 0) for c in combos))
        r[f"stake_{name}"], r[f"return_{name}"] = stake, ret
        r[f"hit_{name}"] = r[f"return_{name}"] > 0
    return r.reset_index()


def summarize(d, n_boot=1000, seed=0):
    """1つの区分の当たり方・回収率。区間はレースを選び直す n_boot 回のブートストラップの95%。"""
    out = {"races": len(d), "small": len(d) < SMALL}
    if not len(d):
        return out
    idx = np.random.default_rng(seed).integers(0, len(d), (n_boot, len(d)))

    def ci(v):
        return float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))

    def mean_ci(a):
        a = np.asarray(a, dtype=float)
        return float(a.mean()), *ci(a[idx].mean(axis=1))

    def ratio_ci(num, den):
        num, den = np.asarray(num, dtype=float), np.asarray(den, dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            return float(num.sum() / den.sum()) if den.sum() else np.nan, *ci(num[idx].sum(1) / den[idx].sum(1))

    for k, act, prob in (("win", "top_win", "top_p_first"), ("top2", "top_top2", "top_p_top2"),
                         ("top3", "top_top3", "top_p_top3")):
        out[f"actual_{k}"] = float(d[act].mean())
        out[f"model_{k}"] = float(d[prob].mean())
        out[f"diff_{k}"], out[f"diff_{k}_lo"], out[f"diff_{k}_hi"] = mean_ci(d[act].astype(float) - d[prob])
    out["model_is_favorite"] = float((d["top_p_rank"] == 1).mean())
    out["lane1_actual_win"] = float(d["lane1_win"].mean())
    out["lane1_model_win"] = float(d["lane1_p_first"].mean())
    names = [n for n, _, _ in BETS] + (["本番"] if "return_本番" in d else [])
    for name in names:
        out[f"roi_{name}"], out[f"roi_{name}_lo"], out[f"roi_{name}_hi"] = ratio_ci(d[f"return_{name}"],
                                                                                   d[f"stake_{name}"])
        out[f"hit_{name}"], out[f"hit_{name}_lo"], out[f"hit_{name}_hi"] = mean_ci(d[f"hit_{name}"])
    return out


def calib_by_prob(d, bins=(0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 1.01)):
    """実力トップの最終の1着確率の帯ごとの、表示と実際。"""
    b = pd.cut(d["top_p_first"], bins, right=False)
    t = d.groupby(b, observed=True).agg(races=("top_win", "size"), model=("top_p_first", "mean"),
                                        actual=("top_win", "mean"))
    t["diff"] = t["actual"] - t["model"]
    return t.reset_index().assign(top_p_first=lambda x: x["top_p_first"].astype(str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--tickets", default="", help="tools/ml_tune.py の race_level_tickets.csv.gz（本番の買い目）")
    ap.add_argument("--features", default="")
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
    _, _, test = ex.split(f, "2y")
    actual = ex.actual_trifecta(test)
    b, tri = boat_table(model, test)
    log(f"[EDGE] 確率 {b['race_key'].nunique()}レース {time.time() - t0:.0f}秒")
    r = race_table(b, tri, payout_table(), actual)
    if args.tickets:
        tk = pd.read_csv(args.tickets)
        tk = tk[tk["model"] == "current"].set_index("race_key")
        r = r[r["race_key"].isin(tk.index)]
        r["stake_本番"] = r["race_key"].map(tk["stake"])
        r["return_本番"] = r["race_key"].map(tk["return"])
        r["hit_本番"] = r["race_key"].map(tk["bet_hit"]).astype(bool)
    r.to_csv(out / "race_level.csv.gz", index=False, encoding="utf-8")
    log(f"[EDGE] 買い目 {len(r)}レース {time.time() - t0:.0f}秒")

    target = r[r["top_lane"] != 1]
    segs = [("全体", "実力トップ1号艇以外", target), ("比較", "実力トップ1号艇", r[r["top_lane"] == 1]),
            ("比較", "全レース", r)]
    segs += [("実力トップの艇番", f"{v}号艇", d) for v, d in target.groupby("top_lane")]
    segs += [("実力差", str(v), d) for v, d in target.groupby("gap_bin", observed=True)]
    segs += [("1号艇の実力順位", f"{int(v)}位", d) for v, d in target.groupby("lane1_la_rank")]
    segs += [("場", str(v), d) for v, d in target.groupby("jcd")]
    segs += [("実力差×艇番", f"{g}|{v}号艇", d) for (g, v), d in target.groupby(["gap_bin", "top_lane"], observed=True)]
    rows = []
    for kind, name, d in segs:
        rows.append({"breakdown": kind, "segment": name, **summarize(d, args.boot)})
        log(f"[EDGE] {kind} {name} {time.time() - t0:.0f}秒")
    pd.DataFrame(rows).to_csv(out / "summary.csv", index=False, encoding="utf-8")
    pd.concat([calib_by_prob(target).assign(segment="実力トップ1号艇以外"),
               calib_by_prob(r[r["top_lane"] == 1]).assign(segment="実力トップ1号艇")]
              ).to_csv(out / "calibration_top.csv", index=False, encoding="utf-8")
    (out / "meta.json").write_text(json.dumps({
        "model": model.version, "test": [ex.TEST_START, ex.TEST_END], "races": len(r), "target_races": len(target),
        "bets": BETS, "gap_bins": GAP_LABELS, "small_races": SMALL, "bootstrap": args.boot,
    }, ensure_ascii=False, indent=1))
    log(f"[EDGE] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
