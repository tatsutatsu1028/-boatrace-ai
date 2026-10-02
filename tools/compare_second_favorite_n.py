"""
second_favorite_n（2番手候補を1着にした買い目の確保）の設定比較（検証専用）。

保存済みスナップショット（固定時の各艇確率 final）から、自動固定と同じ
手順で買い目と資金配分を作り直し、公式の3連単払戻で採点する。
本番の設定・保存データは一切変更しない（読み取りのみ）。

  ① N=0  ② N=1  ③ N=2（全レース）
  ④ 条件付きN=2: 本命の1着確率 <= T1 または 2番手の1着確率 >= T2 のレースだけ
     N=2、それ以外は N=0

入力:
  - SUPABASE_URL / SUPABASE_KEY: prediction_snapshots を読む
  - BOATRACE_DATA_DIR: data/history/k_payouts/<年>/k_payouts_<年月>.csv(.gz)

使い方: python tools/compare_second_favorite_n.py --start 2026-09-08
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prediction import adaptive_ticket_plan, rank_tickets, trifecta  # noqa: E402
from stake_allocator import allocate_stakes_smart  # noqa: E402

BUDGET = 2000
MIN_BET = 100
VALUE_BIAS = 0.0


def load_snapshots(start):
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows, offset, page = [], 0, 200
    while True:
        r = requests.get(
            f"{url}/rest/v1/prediction_snapshots",
            headers={**headers, "Range": f"{offset}-{offset + page - 1}"},
            params={
                "select": "race_key,race_date,collector_name,payload_json",
                "race_date": f"gte.{start}",
                "order": "race_key",
            },
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        rows += batch
        if len(batch) < page:
            break
        offset += page
    return rows


def load_trifecta_payouts(months):
    base = Path(os.environ.get("BOATRACE_DATA_DIR", ".")) / "data/history/k_payouts"
    frames = []
    for ym in months:
        year = ym[:4]
        for name in (f"k_payouts_{ym}.csv.gz", f"k_payouts_{ym}.csv"):
            path = base / year / name
            if path.exists():
                frames.append(pd.read_csv(path, dtype=str))
    if not frames:
        return {}
    k = pd.concat(frames, ignore_index=True)
    k = k[k["bet_type"].eq("3連単")]
    k["payout"] = pd.to_numeric(k["payout"], errors="coerce")
    k = k[k["combo"].str.fullmatch(r"\d-\d-\d", na=False) & k["payout"].notna()]
    out = {}
    for race_key, g in k.groupby("race_key"):
        # 同着で複数組番があれば全て当たり扱い
        out[race_key] = dict(zip(g["combo"], g["payout"].astype(float)))
    return out


def build_tickets(final, n):
    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main_points = min(int(plan["main_n"]), target)
    tickets = rank_tickets(
        tri,
        odds=None,
        main_n=main_points,
        cover_n=target - main_points,
        longshot_n=0,
        hedge_lane=None,  # longshot_n=0 なので保険買い目は発生しない
        use_odds=False,
        first=final,
        min_second_coverage=plan["min_second_coverage"],
        close_third_gap=None,
        close_third_coverage=4,
        second_favorite_n=n,
    )
    return allocate_stakes_smart(
        tickets,
        budget=BUDGET,
        unit=100,
        min_bet=MIN_BET,
        max_longshot_share=0.15,
        max_ticket_share=0.35,
        value_bias=VALUE_BIAS,
        use_odds=False,
        guarantee_col="second_favorite",
    )


def evaluate(final, n, winners):
    t = build_tickets(final, n)
    combos = set(t["combo"])
    staked = t[t["stake"] > 0]
    stake = int(staked["stake"].sum())
    ret = 0.0
    for combo, pay in winners.items():
        hit = staked[staked["combo"].eq(combo)]
        if len(hit):
            ret += float(hit["stake"].iloc[0]) * pay / 100.0
    sf = t["second_favorite"].astype(bool) if "second_favorite" in t else pd.Series(False, index=t.index)
    return {
        "cand_hit": any(c in combos for c in winners),
        "bet_hit": any(c in set(staked["combo"]) for c in winners),
        "stake": stake,
        "ret": ret,
        "n_cand": len(t),
        "n_bet": len(staked),
        "sf_cand": int(sf.sum()),
        "sf_bet": int((sf & (t["stake"] > 0)).sum()),
        "sf_hit": any(
            c in set(t.loc[sf & (t["stake"] > 0), "combo"]) for c in winners
        ),
    }


def summarize(df, label):
    races = len(df)
    stake = df["stake"].sum()
    ret = df["ret"].sum()
    return {
        "設定": label,
        "対象R": races,
        "候補内的中": int(df["cand_hit"].sum()),
        "候補内的中率": df["cand_hit"].mean(),
        "購入的中": int(df["bet_hit"].sum()),
        "購入的中率": df["bet_hit"].mean(),
        "購入額": int(stake),
        "払戻": int(round(ret)),
        "回収率": ret / stake if stake else np.nan,
        "購入点数平均": df["n_bet"].mean(),
        "2番手1着で的中": int(df["sf_hit"].astype(bool).sum()),
    }


def fmt_table(rows):
    df = pd.DataFrame(rows)
    for col in ("候補内的中率", "購入的中率", "回収率", "前半回収率", "後半回収率"):
        if col in df:
            df[col] = df[col].map(lambda v: f"{v * 100:.1f}%" if pd.notna(v) else "-")
    for col in ("購入点数平均",):
        if col in df:
            df[col] = df[col].map(lambda v: f"{v:.2f}")
    for col in ("差額vsN0", "差額SE"):
        if col in df:
            df[col] = df[col].map(lambda v: f"{v:+,.0f}" if col == "差額vsN0" else f"{v:,.0f}")
    return df.to_markdown(index=False) if hasattr(df, "to_markdown") else df.to_string(index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-08")
    ap.add_argument("--out", default="second_favorite_n_report.md")
    args = ap.parse_args()

    snaps = load_snapshots(args.start)
    months = sorted({s["race_date"][:7].replace("-", "-") for s in snaps})
    payouts = load_trifecta_payouts(months)

    records = []
    skipped = {"no_final": 0, "no_payout": 0, "error": 0}
    for s in snaps:
        try:
            payload = json.loads(s["payload_json"])
        except Exception:
            skipped["error"] += 1
            continue
        final = pd.DataFrame(payload.get("final") or [])
        if not len(final) or "p_first" not in final:
            skipped["no_final"] += 1
            continue
        winners = payouts.get(s["race_key"])
        if not winners:
            skipped["no_payout"] += 1
            continue
        try:
            final["lane"] = pd.to_numeric(final["lane"]).astype(int)
            pf = pd.to_numeric(final["p_first"], errors="coerce").fillna(0.0)
            pf_norm = (pf / pf.sum()).sort_values(ascending=False)
            fav_lane = int(final.loc[pf_norm.index[0], "lane"])
            sec_lane = int(final.loc[pf_norm.index[1], "lane"])
            winner_lane = int(next(iter(winners)).split("-")[0])
            base = {
                "race_key": s["race_key"],
                "race_date": s["race_date"],
                "collector": s.get("collector_name"),
                "fav_p": float(pf_norm.iloc[0]),
                "sec_p": float(pf_norm.iloc[1]),
                "pf_sum": float(pf.sum()),
                "winner_is_fav": winner_lane == fav_lane,
                "winner_is_sec": winner_lane == sec_lane,
            }
            for n in (0, 1, 2):
                for k, v in evaluate(final, n, winners).items():
                    base[f"{k}_{n}"] = v
            records.append(base)
        except Exception as e:  # noqa: BLE001
            skipped["error"] += 1
            print("error", s["race_key"], type(e).__name__, e, file=sys.stderr)

    df = pd.DataFrame(records).sort_values(["race_date", "race_key"]).reset_index(drop=True)
    dates = sorted(df["race_date"].unique())
    mid = dates[len(dates) // 2]
    first_half = df["race_date"] < mid

    def view(n_series):
        """各レースで使うNを指定した結果の行列を作る。"""
        cols = ["cand_hit", "bet_hit", "stake", "ret", "n_bet", "sf_hit"]
        out = pd.DataFrame(index=df.index)
        for c in cols:
            out[c] = np.select(
                [n_series.eq(0), n_series.eq(1), n_series.eq(2)],
                [df[f"{c}_0"], df[f"{c}_1"], df[f"{c}_2"]],
            )
        out["cand_hit"] = out["cand_hit"].astype(bool)
        out["bet_hit"] = out["bet_hit"].astype(bool)
        return out

    def row_for(label, n_series, triggered=None):
        v = view(n_series)
        r = summarize(v, label)
        if triggered is not None:
            r["条件該当R"] = int(triggered.sum())
        for name, mask in (("前半回収率", first_half), ("後半回収率", ~first_half)):
            st = v.loc[mask, "stake"].sum()
            r[name] = v.loc[mask, "ret"].sum() / st if st else np.nan
        base = view(pd.Series(0, index=df.index))
        diff = (v["ret"] - v["stake"]) - (base["ret"] - base["stake"])
        r["差額vsN0"] = diff.sum()
        r["差額SE"] = diff.std(ddof=1) * np.sqrt(len(diff))
        return r

    zeros = pd.Series(0, index=df.index)
    main_rows = [
        row_for("① N=0", zeros),
        row_for("② N=1", zeros + 1),
        row_for("③ N=2（全レース）", zeros + 2),
    ]

    late = df["race_date"].ge("2026-09-15")
    late_rows = []
    for n in (0, 1, 2):
        v = view(zeros + n)
        r = summarize(v[late], ["① N=0", "② N=1", "③ N=2（全レース）"][n])
        base = view(zeros)
        diff = ((v["ret"] - v["stake"]) - (base["ret"] - base["stake"]))[late]
        r["差額vsN0"] = diff.sum()
        r["差額SE"] = diff.std(ddof=1) * np.sqrt(len(diff))
        late_rows.append(r)

    cond_rows = []
    for t1 in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        trig = df["fav_p"].le(t1)
        cond_rows.append(row_for(f"本命<= {t1:.2f}", trig.astype(int) * 2, trig))
    for t2 in (0.15, 0.18, 0.20, 0.22, 0.25, 0.30):
        trig = df["sec_p"].ge(t2)
        cond_rows.append(row_for(f"2番手>= {t2:.2f}", trig.astype(int) * 2, trig))
    for t1 in (0.40, 0.45, 0.50, 0.55):
        for t2 in (0.20, 0.25, 0.30):
            trig = df["fav_p"].le(t1) | df["sec_p"].ge(t2)
            cond_rows.append(
                row_for(f"本命<= {t1:.2f} or 2番手>= {t2:.2f}", trig.astype(int) * 2, trig)
            )

    # 本命の1着確率帯ごとの内訳（どこでN=2が効く/効かないか）
    bands = pd.cut(df["fav_p"], [0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 1.0])
    band_rows = []
    for band, idx in df.groupby(bands, observed=True).groups.items():
        sub = df.loc[idx]
        r = {"本命1着確率": str(band), "R数": len(sub),
             "1着=本命": f"{sub['winner_is_fav'].mean() * 100:.1f}%",
             "1着=2番手": f"{sub['winner_is_sec'].mean() * 100:.1f}%"}
        for n in (0, 2):
            st = sub[f"stake_{n}"].sum()
            r[f"購入的中率N{n}"] = f"{sub[f'bet_hit_{n}'].mean() * 100:.1f}%"
            r[f"回収率N{n}"] = f"{sub[f'ret_{n}'].sum() / st * 100:.1f}%" if st else "-"
        band_rows.append(r)

    lines = [
        "# second_favorite_n 比較",
        "",
        f"- 期間: {df['race_date'].min()} 〜 {df['race_date'].max()}（前半/後半の境: {mid}）",
        f"- 対象: {len(df)}レース（スナップショット {len(snaps)}件、除外 {skipped}）",
        f"- 収集者内訳: {df['collector'].value_counts().to_dict()}",
        f"- p_first 合計の範囲: {df['pf_sum'].min():.3f}〜{df['pf_sum'].max():.3f}（条件判定は合計1に正規化）",
        f"- 1着=本命: {df['winner_is_fav'].mean() * 100:.1f}% / 1着=2番手: {df['winner_is_sec'].mean() * 100:.1f}%",
        f"- 2番手1着の買い目（N=2）の的中: {int(df['sf_hit_2'].sum())}R / N=1: {int(df['sf_hit_1'].sum())}R",
        f"- 配分: 予算{BUDGET}円 最低{MIN_BET}円 value_bias={VALUE_BIAS}、2番手1着は最低額保証",
        "- 差額vsN0: N=0と比べた収支（払戻-購入）の差。差額SE: その標準誤差（目安）",
        "",
        "## ①〜③",
        "",
        fmt_table(main_rows),
        "",
        "## ①〜③（9/15以降のみ: 3着条件付き確率が保存されているスナップショット）",
        "",
        fmt_table(late_rows),
        "",
        "## ④ 条件付きN=2（該当レースだけN=2、それ以外N=0）",
        "",
        fmt_table(cond_rows),
        "",
        "## 本命の1着確率帯ごとの内訳",
        "",
        pd.DataFrame(band_rows).to_markdown(index=False),
        "",
    ]
    report = "\n".join(lines)
    Path(args.out).write_text(report, encoding="utf-8")
    df.to_csv(Path(args.out).with_suffix(".csv"), index=False)
    print(report)


if __name__ == "__main__":
    main()
