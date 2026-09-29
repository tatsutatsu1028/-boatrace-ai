"""一時（確認用）: 5/1・11/1 をまたぐ節で、出走表の選手の値が境界の前後で変わるかを確かめる。

各境界（2024-11-01, 2025-05-01, 2025-11-01, 2026-05-01）をまたぐ節を1つずつ選び、
境界の前後両方を走った選手が載っているレースの出走表を取得して比べる。
ログには「変わった選手数」だけを出し、データそのものは出さない。
"""
from datetime import datetime, timedelta

import pandas as pd

import history_store as store
from history_pages import parse_racelist, _fetch

COLS = ["racer_class", "avg_st", "f_count", "l_count", "national_win_rate", "national_2ren",
        "national_3ren", "local_win_rate", "local_2ren", "local_3ren", "motor_no", "motor_2ren",
        "motor_3ren", "boat_no", "boat_2ren", "boat_3ren"]

b = store.read_kind("b_programs")
b["meet"] = [(datetime.strptime(d, "%Y%m%d") - timedelta(days=int(n) - 1)).strftime("%Y%m%d")
             for d, n in zip(b.race_date, b.day_no)]
span = b.groupby(["jcd", "meet"]).race_date.agg(["min", "max"]).reset_index()
total = {c: 0 for c in COLS}
racers = 0
for bd in ("20241101", "20250501", "20251101", "20260501"):
    cand = span[(span["min"] < bd) & (span["max"] >= bd)]
    if cand.empty:
        continue
    j, m = cand.iloc[0]["jcd"], cand.iloc[0]["meet"]
    part = b[(b.jcd == j) & (b.meet == m)]
    before = part[part.race_date < bd]
    after = part[part.race_date >= bd]
    # 境界直前日・直後日のレースで、両方を走る選手が多いレースを2つずつ
    d0, d1 = before.race_date.max(), after.race_date.min()
    both = set(before[before.race_date == d0].racer_id) & set(after[after.race_date == d1].racer_id)
    pick = []
    for d in (d0, d1):
        day = part[part.race_date == d]
        cnt = day[day.racer_id.isin(both)].groupby("race_key").size().sort_values(ascending=False)
        pick += list(cnt.index[:3])
    vals = {}
    for rk in pick:
        hd, jcd, rno = rk.split("_")
        race, boats = parse_racelist(_fetch("racelist", hd, jcd, rno), hd, rno)
        for ln, rec in boats.items():
            rid = rec.get("racer_id")
            if rid in both:
                vals.setdefault(rid, {})[hd] = rec
    changed = {c: 0 for c in COLS}
    n = 0
    for rid, byday in vals.items():
        if len(byday) < 2:
            continue
        n += 1
        a, z = byday[min(byday)], byday[max(byday)]
        for c in COLS:
            if str(a.get(c)) != str(z.get(c)):
                changed[c] += 1
    racers += n
    for c in COLS:
        total[c] += changed[c]
    print(f"[CHECK] 境界 {bd} 場{j} 節初日{m}: 前後を比べた選手 {n}人 変わった項目 "
          f"{ {c: v for c, v in changed.items() if v} }", flush=True)
print(f"[CHECK] 合計 {racers}人 変わった項目 { {c: v for c, v in total.items() if v} }", flush=True)
