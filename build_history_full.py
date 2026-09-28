"""
data/history/ に集めた過去データから、history_full.csv と同じ形式の行を作る。

用途:
  1) history_full.csv の空白期間（2026-06-25〜09-07）の補完
       python build_history_full.py --start 20260625 --end 20260907
  2) 既存行への finish_full（実際の1〜6着）の後付け
       python build_history_full.py --fill-finish-full

history_full.csv の finish は既存の学習・コース基準値と互換のため
「1〜3着はそのまま、それ以外は4」のまま作る。実際の着順は finish_full。
出走表・直前情報は pages、着順・3連単・決まり手は競走成績（k_results / k_payouts）から取る。
3連単が不成立・特払いのレースは、既存の収集と同じく作らない。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import history_store as store
from data_paths import data_path
from collect_history import _flush

HIST = data_path("history_full.csv")

PAGE_COLS = {
    "racer_id": "racer_id", "racer_name": "racer_name", "racer_class": "racer_class",
    "avg_st": "avg_st", "racer_win_rate": "racer_win_rate", "local_win_rate": "local_win_rate",
    "motor_2ren": "motor_2ren", "boat_2ren": "boat_2ren",
    "racer_name_beforeinfo": "racer_name_beforeinfo", "weight": "weight",
    "exhibition_time": "exhibition_time", "tilt": "tilt", "exhibition_st": "exhibition_st",
    "temperature": "temperature", "wind_speed": "wind_speed",
    "water_temperature": "water_temperature", "wave_cm": "wave_height",
    "f_count": "f_count", "l_count": "l_count",
    "parts_exchange": "parts_exchange", "parts_exchanged": "parts_exchanged",
}


def build_rows(start, end):
    pages = store.read_kind("pages", start, end)
    k = store.read_kind("k_results", start, end)
    pay = store.read_kind("k_payouts", start, end)
    if pages.empty or k.empty:
        return pd.DataFrame()
    tri = pay[(pay["bet_type"] == "3連単") & pay["combo"].str.fullmatch(r"[1-6]-[1-6]-[1-6]")]
    tri = tri.drop_duplicates("race_key")[["race_key", "combo", "payout"]].rename(
        columns={"combo": "trifecta", "payout": "trifecta_payout_per_100"})
    kk = k[["race_key", "lane", "finish", "kimarite", "venue"]].copy()
    kk["finish_full"] = pd.to_numeric(kk["finish"], errors="coerce").astype("Int64")
    df = pages.merge(kk.drop(columns="finish"), on=["race_key", "lane"], how="inner")
    df = df.merge(tri, on="race_key", how="inner")
    out = pd.DataFrame({"lane": pd.to_numeric(df["lane"]).astype(int)})
    for src, dst in PAGE_COLS.items():
        out[dst] = df[src] if src in df.columns else pd.NA
    ff = df["finish_full"]
    out["finish"] = ff.where(ff.isin([1, 2, 3]), 4).fillna(4).astype(int)
    out["race_date"] = df["race_date"]
    out["jcd"] = df["jcd"].astype(str).str.zfill(2)
    out["venue"] = df["venue"]
    out["race_no"] = pd.to_numeric(df["race_no"]).astype(int)
    out["race_key"] = df["race_key"]
    out["trifecta"] = df["trifecta"]
    out["trifecta_payout_per_100"] = df["trifecta_payout_per_100"]
    out["kimarite"] = df["kimarite"]
    out["finish_full"] = ff
    # 1〜3着が揃っていて3連単と一致するレースだけ残す（既存収集と同じ条件）
    top = out[out["finish"] <= 3].pivot_table(index="race_key", columns="finish", values="lane", aggfunc="first")
    tri_of = out.drop_duplicates("race_key").set_index("race_key")["trifecta"]
    ok = set()
    for key, r in top.iterrows():
        if all(c in r.index and pd.notna(r[c]) for c in (1, 2, 3)) \
                and f"{int(r[1])}-{int(r[2])}-{int(r[3])}" == tri_of[key]:
            ok.add(key)
    return out[out["race_key"].isin(ok)].sort_values(["race_date", "jcd", "race_no", "lane"])


def append_range(start, end):
    rows = build_rows(start, end)
    have = set(pd.read_csv(HIST, usecols=["race_key"], dtype=str)["race_key"]) if HIST.exists() else set()
    rows = rows[~rows["race_key"].isin(have)]
    if rows.empty:
        print("[BUILD] 追加する行はありません")
        return 0
    _flush([rows], [], str(HIST))
    print(f"[BUILD] {rows['race_key'].nunique()}レース {len(rows)}行を追加 "
          f"({rows['race_date'].min()}〜{rows['race_date'].max()})")
    return rows["race_key"].nunique()


def fill_finish_full():
    """既存の行に finish_full（実着順）を競走成績から埋める。finish は変えない。"""
    hist = pd.read_csv(HIST, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    k = store.read_kind("k_results", columns=["race_key", "lane", "finish"])
    m = dict(zip(zip(k["race_key"], k["lane"].astype(str)), k["finish"]))
    if "finish_full" not in hist.columns:
        hist["finish_full"] = ""
    blank = hist["finish_full"].astype(str).str.strip() == ""
    fill = [m.get((rk, str(ln)), "") for rk, ln in zip(hist["race_key"], hist["lane"])]
    fill = [str(int(float(v))) if str(v).strip() not in ("", "nan") else "" for v in fill]
    hist.loc[blank, "finish_full"] = pd.Series(fill, index=hist.index)[blank]
    # 整合チェック: 1〜3着は既存の finish と一致するはず
    f = pd.to_numeric(hist["finish"], errors="coerce")
    ff = pd.to_numeric(hist["finish_full"], errors="coerce")
    bad = ((ff <= 3) & (f != ff)) | ((f <= 3) & ff.notna() & (f != ff))
    hist.to_csv(HIST, index=False, encoding="utf-8-sig")
    print(f"[BUILD] finish_full 埋めた行 {int((blank & (ff.notna())).sum())} / 空欄のまま {int(ff.isna().sum())} "
          f"/ finishと不一致 {int(bad.sum())}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--fill-finish-full", action="store_true")
    a = ap.parse_args()
    if a.start and a.end:
        append_range(a.start, a.end)
    if a.fill_finish_full:
        fill_finish_full()


if __name__ == "__main__":
    main()
