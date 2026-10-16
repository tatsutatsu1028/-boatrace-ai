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


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("rates")
    a.add_argument("--out", required=True)
    a.add_argument("--start", default="")
    a.add_argument("--end", default="")
    args = ap.parse_args()
    {"rates": cmd_rates}[args.cmd](args)


if __name__ == "__main__":
    main()
