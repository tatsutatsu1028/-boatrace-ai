"""
複数会場の出走表PDFから、その日の朝の予想を全レース分出し、厳選と万舟の候補を選ぶ（試作）。

PDFからは「何Rの何号艇が誰か」と締切だけを読み（pdf_generic）、選手の成績は手元の過去データの最新の値を使う:
  - 番組表の値（級別・年齢・体重・全国勝率/2連率）: 各選手の最新の番組表（data/history/b_programs）
  - 当地勝率/2連率: その会場の最新の番組表の値。無ければ当地勝率だけ過去の成績の着順点の平均（pdf_predict.local_win_rates）
  - 出走表ページの値（平均ST・F/L・全国3連率・当地3連率）: 各選手の最新の出走表（data/history/pages）
  - モーター・ボート: 分からないので空（モデルは欠けた値として扱う）
  - 今節のこれまでの走: 無し（手元の成績は前の日まで）
常滑のように詳しく読める会場（pdf_racecard）は、その値を優先して使う。公式サイトにはアクセスしない。

  BOATRACE_DATA_DIR=../-boatrace-ai-data python pdf_predict_generic.py venues.json
venues.json: {"date": "20261125", "venues": [{"jcd": "05", "venue": "多摩川", "grade": "一般", "day_no": 5,
               "final_day": false, "pdfs": [...], "detailed": false}, ...]}
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd


def _latest(df, keys):
    return df.sort_values("race_date").drop_duplicates(keys, keep="last")


def build_inputs(date, venues, b_hist, pages_hist, k):
    import pdf_generic
    import pdf_racecard
    from pdf_predict import local_win_rates

    known = set(b_hist["racer_id"])
    lb = _latest(b_hist, ["racer_id"]).set_index("racer_id")
    lb_local = _latest(b_hist, ["racer_id", "jcd"]).set_index(["racer_id", "jcd"])
    lp = _latest(pages_hist, ["racer_id"]).set_index("racer_id")
    lp_local = _latest(pages_hist, ["racer_id", "jcd"]).set_index(["racer_id", "jcd"])
    progs, rls, runs_all = [], [], []
    for v in venues:
        jcd = str(v["jcd"]).zfill(2)
        if v.get("detailed"):
            parts = [pdf_racecard.parse_pdf(p, date, jcd, v["venue"], grade=v["grade"], day_no=v["day_no"])
                     for p in v["pdfs"]]
            p = pd.concat([x[0] for x in parts], ignore_index=True)
            lw = local_win_rates(k, date, jcd)
            p["local_win_rate"] = p["racer_id"].map(lw)
            progs.append(p)
            rls.append(pd.concat([x[1] for x in parts], ignore_index=True))
            runs_all.append(pd.concat([x[2] for x in parts], ignore_index=True))
            continue
        races = pdf_generic.read_venue(v["pdfs"], known)
        lw = local_win_rates(k, date, jcd)
        for r in races:
            rk = f"{date}_{jcd}_{r['race_no']}"
            for lane, rid in enumerate(r["ids"], start=1):
                b = lb.loc[rid] if rid in lb.index else None
                bl = lb_local.loc[(rid, jcd)] if (rid, jcd) in lb_local.index else None
                pg = lp.loc[rid] if rid in lp.index else None
                pgl = lp_local.loc[(rid, jcd)] if (rid, jcd) in lp_local.index else None

                def g(row, c):
                    return row[c] if row is not None and c in row.index else np.nan

                progs.append(pd.DataFrame([{
                    "race_date": date, "jcd": jcd, "venue": v["venue"], "race_no": r["race_no"], "race_key": rk,
                    "meet_title": v.get("meet_title", v["venue"]), "day_no": v["day_no"], "race_type": "予選",
                    "fixed_entry": 0, "distance_m": 1800, "deadline": r["deadline"], "lane": lane,
                    "racer_id": rid, "racer_name": g(b, "racer_name") if b is not None else rid,
                    "age": g(b, "age"), "branch": g(b, "branch"), "weight": g(b, "weight"),
                    "racer_class": g(b, "racer_class"), "national_win_rate": g(b, "national_win_rate"),
                    "national_2ren": g(b, "national_2ren"),
                    "local_win_rate": g(bl, "local_win_rate") if bl is not None else lw.get(rid, np.nan),
                    "local_2ren": g(bl, "local_2ren"),
                    "motor_no": "", "motor_2ren": np.nan, "boat_no": "", "boat_2ren": np.nan,
                    "meet_results_raw": "", "hayami": "",
                }]))
                rls.append(pd.DataFrame([{
                    "race_key": rk, "lane": lane, "grade": v["grade"], "is_final_day": int(bool(v.get("final_day"))),
                    "avg_st": g(pg, "avg_st"), "f_count": g(pg, "f_count"), "l_count": g(pg, "l_count"),
                    "national_3ren": g(pg, "national_3ren"), "local_3ren": g(pgl, "local_3ren"),
                    "motor_3ren": np.nan, "boat_3ren": np.nan,
                }]))
    programs = pd.concat(progs, ignore_index=True)
    racelists = pd.concat(rls, ignore_index=True)
    for c in ("age", "weight", "national_win_rate", "national_2ren", "local_win_rate", "local_2ren",
              "motor_2ren", "boat_2ren"):
        programs[c] = pd.to_numeric(programs[c], errors="coerce")
    for c in ("avg_st", "f_count", "l_count", "national_3ren", "local_3ren", "motor_3ren", "boat_3ren"):
        racelists[c] = pd.to_numeric(racelists[c], errors="coerce")
    runs = pd.concat(runs_all, ignore_index=True) if runs_all else pd.DataFrame()
    return programs, racelists, runs


def predict_day(cfg):
    import history_store as store
    import manshu
    import ml_live
    import ml_model as mm
    import ml_pre
    from auto_random_fix import _runtime_settings
    from data_paths import data_path
    from pdf_predict import meet_events
    from pre_picks import build_tickets, risk_race

    date = cfg["date"]
    b_hist = store.read_kind("b_programs", "20260101")
    pages_hist = store.read_kind("pages", "20260101")
    for d in (b_hist, pages_hist):
        d["racer_id"] = d["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        d["jcd"] = d["jcd"].astype(str).str.zfill(2)
        d["race_date"] = d["race_date"].astype(str)
    k = store.read_kind("k_results", "20240927")
    k["racer_id"] = k["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    programs, racelists, runs = build_inputs(date, cfg["venues"], b_hist, pages_hist, k)

    start = (pd.Timestamp(date) - pd.Timedelta(days=12)).strftime("%Y%m%d")
    recent = k[k["race_date"].astype(str) >= start].copy()
    if len(runs):
        ev = meet_events(programs[programs["jcd"].isin(
            [str(v["jcd"]).zfill(2) for v in cfg["venues"] if v.get("detailed")])], runs, date)
        recent = pd.concat([recent, ev.astype({"racer_id": str})], ignore_index=True)
    recent["race_key"] = recent["race_key"].astype(str)
    recent["lane"] = pd.to_numeric(recent["lane"], errors="coerce").astype("Int64")

    version = ml_pre.latest_version()
    model = mm.ChainModel.load(data_path(ml_live.model_file(version)))
    state = ml_live.load_state()
    feats = ml_pre.live_pre_table(programs, racelists, state, recent)
    feats = feats[feats.groupby("race_key")["lane"].transform("count") == 6]
    runtime = _runtime_settings({})
    names = {rk: dict(zip(g["lane"].astype(int), g["racer_name"].astype(str))) for rk, g in programs.groupby("race_key")}
    info = programs.drop_duplicates("race_key").set_index("race_key")
    rows = []
    for rk, f in feats.groupby("race_key", sort=False):
        f = f.reset_index(drop=True)
        final = ml_pre.finals(model, f, names)[rk]
        tickets, hp = build_tickets(final, risk_race(f), runtime)
        p1 = final.set_index("lane")["p_first"].astype(float)
        fav = int(p1.idxmax())
        rows.append({"race_key": rk, "venue": info.at[rk, "venue"], "race_no": int(info.at[rk, "race_no"]),
                     "deadline": info.at[rk, "deadline"], "hit_probability": float(hp), "favorite_lane": fav,
                     "favorite_name": names[rk].get(fav, ""), "favorite_prob": float(p1.max()),
                     "tickets": " ".join(t["combo"] for t in tickets.to_dict("records"))})
    cands = pd.DataFrame(rows)
    probs, longs, _ = manshu.predict_with_chain("pre", model, feats)
    cands["manshu_prob"] = cands["race_key"].map(probs).astype(float)
    cands["longshot"] = cands["race_key"].map(
        lambda rk: " ".join(t["combo"] if isinstance(t, dict) else str(t) for t in longs.get(rk, [])))
    return cands, programs


if __name__ == "__main__":
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    cands, _ = predict_day(cfg)
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if out:
        cands.to_csv(out, index=False)
    pd.set_option("display.width", 250)
    print(len(cands), "races")
    print(cands.sort_values("hit_probability", ascending=False).head(10)
          [["venue", "race_no", "deadline", "favorite_lane", "favorite_name", "favorite_prob", "hit_probability"]]
          .to_string(index=False))
    print(cands.sort_values("manshu_prob", ascending=False).head(10)
          [["venue", "race_no", "deadline", "manshu_prob", "longshot"]].to_string(index=False))
