"""
「1号艇が負けた場合の着順」専用モデル（ml_upset.py）の検証。本番には使わない。

今の新モデルの検証（tools/ml_experiment.py）と同じ期間で分ける:
  学習 〜2026-06-06 / 確率の調整（温度）2026-06-07〜07-06 / テスト 2026-07-07〜10-06
比べる相手は同じ期間で学習した今の新モデル（reports/ml_v2 の ml-chain-v2-lgb-2y）と展示前モデル
（reports/pre_exhibition の ml-chain-pre-v2-lgb-2y）。専用モデルの3連単は ml_upset.combine
（1号艇1着の組は今のモデルのまま、それ以外を（1−1号艇の1着確率）× 専用モデル）。

テスト期間の、1号艇が負けたレースで:
  - 1号艇以外の1番手（1着確率が最も高い艇）が1着になった割合 / 上位2艇のどちらかが1着の割合
  - 実際の組の3連単確率の順位（120通り中。1が最上位）。万舟（払戻1万円以上）のレースでも
  - 実際の1着艇が1着のときの、2着の1番手が当たった割合
  - 表示する確率と実際の割合（1号艇が負けたときの1着確率・全レースの1着確率・3連単確率）
を、全レースと「万舟チャンスに選ばれたレース」（万舟モデルの展示前版の確率で毎日上位10レース。
reports/manshu/race_level_pre.csv.gz）で出す。全レースの3連単の LogLoss も出す（悪くしていないか）。

結果は公式データから計算したものなので --out（非公開のデータ用リポジトリ）にだけ書く。標準出力は進み具合だけ。

  BOATRACE_DATA_DIR=<データ用リポジトリ> python tools/ml_upset_experiment.py --out <データ用リポジトリ>/reports/upset_v1 \
      --features-post feats_post.pkl --features-pre feats_pre.pkl
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
import ml_pre  # noqa: E402
import ml_upset  # noqa: E402
from ml_experiment import CALIB_START, TEST_END, TEST_START  # noqa: E402

BINS = [0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0001]
TRI_BINS = [0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2, 1.0001]


def log(*a):
    print(*a, flush=True)


def split(f):
    return (f[f["race_date"] < CALIB_START], f[(f["race_date"] >= CALIB_START) & (f["race_date"] < TEST_START)],
            f[(f["race_date"] >= TEST_START) & (f["race_date"] <= TEST_END)])


def payouts():
    p = store.read_kind("k_payouts", TEST_START, TEST_END)
    p = p[p["bet_type"].astype(str).str.contains("3連単")]
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    return p.groupby("race_key")["payout"].max().to_dict()


def actual(test):
    t = test[test["finish"].isin([1, 2, 3])].sort_values("finish")
    g = t.groupby("race_key")["lane"].apply(lambda s: "-".join(str(int(v)) for v in s) if len(s) == 3 else None)
    return g.dropna()


def manshu_picks(path, n=10):
    """万舟チャンスに選ばれたレース（展示前版の万舟確率で毎日上位 n レース）。"""
    d = pd.read_csv(path, dtype={"race_key": str, "race_date": str})
    d = d.sort_values(["race_date", "p_full"], ascending=[True, False])
    return set(d.groupby("race_date").head(n)["race_key"])


def tables(model, test):
    first, second, third = model.predict_tables(test)
    return first, second, third, mm.trifecta_table(first, second, third)


def race_rows(name, first, second, tri, act):
    """1レース1行。first: 1号艇以外の1着確率（1号艇が負けたときの割合に直したもの）、second: 2着の条件付き確率。"""
    tri = tri.copy()
    tri["rank"] = tri.groupby("race_key")["prob"].rank(ascending=False, method="average")
    tri_act = tri.merge(act.rename("combo").reset_index(), on=["race_key", "combo"])
    f = first[first["lane"] != 1].copy()
    f["p"] = f["p_first"] / f.groupby("race_key")["p_first"].transform("sum")
    f = f.sort_values(["race_key", "p", "lane"], ascending=[True, False, True])
    top = f.groupby("race_key").head(2).groupby("race_key")["lane"].apply(list)
    win = act.str[0].astype(int)
    p_win = f.merge(win.rename("lane").reset_index(), on=["race_key", "lane"]).set_index("race_key")["p"]
    sec = act.str[2].astype(int)
    s = second.merge(win.rename("w_lane").reset_index(), on=["race_key", "w_lane"])
    s = s.sort_values(["race_key", "p", "lane"], ascending=[True, False, True])
    s_top = s.groupby("race_key").head(1).set_index("race_key")["lane"]
    out = pd.DataFrame({"actual": act})
    out["winner"] = win
    out["top"] = top
    out["top1_hit"] = [bool(t) and t[0] == w for t, w in zip(out["top"].reindex(out.index), out["winner"])]
    out["top2_hit"] = [isinstance(t, list) and w in t for t, w in zip(out["top"].reindex(out.index), out["winner"])]
    out["p_winner_cond"] = p_win.reindex(out.index)
    out["second_top_hit"] = (s_top.reindex(out.index) == sec.reindex(out.index))
    out["p_actual"] = tri_act.set_index("race_key")["prob"].reindex(out.index)
    out["rank_actual"] = tri_act.set_index("race_key")["rank"].reindex(out.index)
    out["model"] = name
    return out.reset_index().drop(columns="top")


def calib(p, y, bins):
    b = pd.cut(p, bins, right=False)
    t = pd.DataFrame({"p": p, "y": y, "b": b}).groupby("b", observed=True).agg(
        n=("y", "size"), shown=("p", "mean"), actual=("y", "mean"))
    t["gap"] = t["actual"] - t["shown"]
    return t.reset_index().assign(b=lambda d: d["b"].astype(str))


def summarize(df, segs):
    rows = []
    for seg, keys in segs.items():
        for m, d in df.groupby("model"):
            d = d[d["race_key"].isin(keys)]
            up = d[d["winner"] != 1]
            man = up[up["manshu"]]
            rows.append({
                "segment": seg, "model": m, "races": len(d), "lane1_lost": len(up),
                "top1_hit": up["top1_hit"].mean(), "top2_hit": up["top2_hit"].mean(),
                "avg_rank": up["rank_actual"].mean(), "median_rank": up["rank_actual"].median(),
                "manshu_races": len(man), "manshu_avg_rank": man["rank_actual"].mean(),
                "cond_first_logloss": -np.log(np.clip(up["p_winner_cond"], 1e-9, None)).mean(),
                "second_top_hit": up["second_top_hit"].mean(),
                "tri_logloss_lane1_lost": -np.log(np.clip(up["p_actual"].fillna(1e-9), 1e-9, None)).mean(),
                "tri_logloss_all": -np.log(np.clip(d["p_actual"].fillna(1e-9), 1e-9, None)).mean(),
                "top10_in_lane1_lost": (up["rank_actual"] <= 10).mean(),
                "top20_in_lane1_lost": (up["rank_actual"] <= 20).mean(),
            })
    return pd.DataFrame(rows)


def evaluate(kind, base, upset, test, pay, picks, out):
    """kind: post / pre。今のモデル（base）と、専用モデルで置き換えた3連単（upset）を同じレースで比べる。"""
    t0 = time.time()
    act = actual(test)
    b1, b2, _, btri = tables(base, test)
    log(f"[UPSET-EXP] {kind} 今のモデル {time.time() - t0:.0f}秒")
    u1, u2, _, utri = tables(upset, test)
    log(f"[UPSET-EXP] {kind} 専用モデル {time.time() - t0:.0f}秒")
    act = act[act.index.isin(set(btri["race_key"]) & set(utri["race_key"]))]

    # 合わせた3連単: 1号艇1着の組は今のモデル、それ以外は (1 − P1) × 専用モデル
    p1 = btri[btri["combo"].str[0] == "1"].groupby("race_key")["prob"].sum()
    ctri = pd.concat([btri[btri["combo"].str[0] == "1"],
                      utri.assign(prob=utri["prob"] * (1 - utri["race_key"].map(p1).fillna(0.0)))],
                     ignore_index=True)
    # 1着: 1号艇以外の割合は専用モデル（今のモデルの1号艇の確率はそのまま）
    cfirst = pd.concat([b1[b1["lane"] == 1], u1.assign(p_first=u1["p_first"] * (1 - u1["race_key"].map(p1)))],
                       ignore_index=True)
    csecond = pd.concat([b2[b2["w_lane"] == 1], u2], ignore_index=True)

    rows = pd.concat([race_rows("current", b1, b2, btri, act), race_rows("upset", cfirst, csecond, ctri, act)],
                     ignore_index=True)
    rows["payout"] = rows["race_key"].map(pay)
    rows["manshu"] = rows["payout"] >= 10000
    rows["kind"] = kind
    keys = set(act.index)
    segs = {"テスト期間の全レース": keys, "万舟チャンス（毎日上位10）": keys & picks}
    summ = summarize(rows, segs).assign(kind=kind)

    # 表示する確率と実際の割合
    cal = []
    lost = act[act.str[0] != "1"]
    for name, fst in (("current", b1), ("upset", cfirst)):
        f = fst[fst["lane"] != 1].copy()
        f["p"] = f["p_first"] / f.groupby("race_key")["p_first"].transform("sum")
        for seg, ks in segs.items():
            x = f[f["race_key"].isin(set(lost.index) & ks)]
            y = (x["lane"] == x["race_key"].map(lost.str[0].astype(int))).astype(float)
            cal.append(calib(x["p"].to_numpy(), y.to_numpy(), BINS).assign(
                model=name, segment=seg, what="1号艇が負けたときの1着確率"))
            x = fst[fst["race_key"].isin(ks)]
            y = (x["lane"] == x["race_key"].map(act.str[0].astype(int))).astype(float)
            cal.append(calib(x["p_first"].to_numpy(), y.to_numpy(), BINS).assign(
                model=name, segment=seg, what="1着確率（全レース）"))
    for name, tri in (("current", btri), ("upset", ctri)):
        for seg, ks in segs.items():
            x = tri[tri["race_key"].isin(ks)]
            y = (x["combo"] == x["race_key"].map(act)).astype(float)
            cal.append(calib(x["prob"].to_numpy(), y.to_numpy(), TRI_BINS).assign(
                model=name, segment=seg, what="3連単確率（全レース）"))
            x = x[x["race_key"].isin(set(lost.index)) & (x["combo"].str[0] != "1")]
            cal.append(calib(x["prob"].to_numpy() / (1 - x["race_key"].map(p1)).to_numpy(),
                             (x["combo"] == x["race_key"].map(act)).astype(float).to_numpy(), TRI_BINS).assign(
                model=name, segment=seg, what="3連単確率（1号艇が負けたレース・1号艇が負ける確率で割った値）"))
    cal = pd.concat(cal, ignore_index=True).assign(kind=kind)
    rows.to_csv(out / f"race_level_{kind}.csv.gz", index=False, encoding="utf-8")
    return summ, cal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--features-post", default="")
    ap.add_argument("--features-pre", default="")
    ap.add_argument("--base-post", default="", help="既定: <データ>/reports/ml_v2/models/ml-chain-v2-lgb-2y.joblib")
    ap.add_argument("--base-pre", default="",
                    help="既定: <データ>/reports/pre_exhibition/models/ml-chain-pre-v2-lgb-2y.joblib")
    ap.add_argument("--manshu", default="", help="既定: <データ>/reports/manshu/race_level_pre.csv.gz")
    ap.add_argument("--kinds", default="post,pre")
    ap.add_argument("--reuse", action="store_true")
    args = ap.parse_args()
    from data_paths import data_path

    out = Path(args.out)
    (out / "models").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    pay = payouts()
    picks = manshu_picks(args.manshu or data_path("reports/manshu/race_level_pre.csv.gz"))
    srcs = None
    summ, cal, info = [], [], {"periods": {"train_to": CALIB_START, "calib": [CALIB_START, TEST_START],
                                          "test": [TEST_START, TEST_END]}}
    for kind in args.kinds.split(","):
        path = args.features_post if kind == "post" else args.features_pre
        if path and Path(path).exists():
            f = pd.read_pickle(path)
        else:
            srcs = srcs or mf.load_sources(ml_upset.HISTORY_START)
            f = mf.build_table(*srcs) if kind == "post" else ml_pre.build_pre_table(*srcs)
        train, calib_f, test = split(f)
        version = f"{ml_upset.UPSET_FAMILY if kind == 'post' else ml_upset.UPSET_PRE_FAMILY}-{ml_upset.MODEL_SET}-exp"
        mpath = out / "models" / f"{version}.joblib"
        if args.reuse and mpath.exists():
            model = ml_upset.UpsetModel.load(mpath)
        else:
            model = (ml_upset.new_model if kind == "post" else ml_upset.new_pre_model)(version)
            model.fit(train, calib_f, log=log)
            model.save(mpath)
        log(f"[UPSET-EXP] {kind} 学習 {time.time() - t0:.0f}秒")
        base_path = (args.base_post or data_path("reports/ml_v2/models/ml-chain-v2-lgb-2y.joblib")) if kind == "post" \
            else (args.base_pre or data_path("reports/pre_exhibition/models/ml-chain-pre-v2-lgb-2y.joblib"))
        base = mm.ChainModel.load(base_path)
        s, c = evaluate(kind, base, model, test, pay, picks, out)
        summ.append(s)
        cal.append(c)
        info[kind] = dict(model.info, temps=model.temps, base=base.version, base_info=base.info)
    pd.concat(summ, ignore_index=True).to_csv(out / "summary.csv", index=False, encoding="utf-8")
    pd.concat(cal, ignore_index=True).to_csv(out / "calibration.csv", index=False, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str))
    log(f"[UPSET-EXP] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
