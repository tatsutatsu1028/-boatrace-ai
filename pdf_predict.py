"""
手で集めた会場の出走表PDFから、朝の予想（展示前モデル）と万舟の確率を出す（試作）。

公式サイトにはアクセスしない。使うのは PDF・データ用リポジトリの過去データ（成績・集計表）・学習済みモデルだけ。
オッズは使わない。

  BOATRACE_DATA_DIR=../-boatrace-ai-data python pdf_predict.py 20261125 08 常滑 SG 2 path/to/08_20261125.pdf

PDFに無い値:
  - 当地勝率: データ用リポジトリの競走成績から、その会場の着順点（1着10・2着8・3着6・4着4・5着2・6着1）の平均で代わりに作る
  - モーター・ボートの3連率: 空のまま（モデルは欠けた値として扱う）
  - 今節の前日までの走: PDFの「今節成績」欄から作る（レース番号は分からないので並び順で代わりにする）
"""
from __future__ import annotations

import sys

import pandas as pd

PTS = {1: 10, 2: 8, 3: 6, 4: 4, 5: 2, 6: 1}


def local_win_rates(k, date, jcd, years=2):
    """会場ごとの着順点の平均（当地勝率の代わり）{racer_id: 値}。"""
    start = (pd.Timestamp(date) - pd.DateOffset(years=years)).strftime("%Y%m%d")
    kk = k[(k["race_date"].astype(str) >= start) & (k["race_date"].astype(str) < str(date))
           & (k["jcd"].astype(str).str.zfill(2) == str(jcd).zfill(2))].copy()
    kk["pt"] = pd.to_numeric(kk["finish"], errors="coerce").map(PTS)
    return kk.groupby("racer_id")["pt"].mean().round(2).to_dict()


def meet_events(programs, runs, date):
    """今節の前日までの走（PDFの今節成績）を、競走成績と同じ形の行にする。"""
    if runs is None or runs.empty:
        return pd.DataFrame()
    info = programs.drop_duplicates("racer_id").set_index("racer_id")
    rows = []
    prev = (pd.Timestamp(date) - pd.Timedelta(days=1)).strftime("%Y%m%d")
    for r in runs.to_dict("records"):
        p = info.loc[r["racer_id"]]
        rno = int(r["n"])
        rows.append({
            "race_date": prev, "jcd": p["jcd"], "venue": p["venue"], "race_no": rno,
            "race_key": f"{prev}_{p['jcd']}_{rno}", "meet_title": p["meet_title"],
            "day_no": (int(p["day_no"]) - 1) if pd.notna(p["day_no"]) else None,
            "race_type": p["race_type"], "fixed_entry": 0, "distance_m": p["distance_m"],
            "lane": int(r["course"]), "racer_id": r["racer_id"], "racer_name": p["racer_name"],
            "finish": r["finish"], "finish_raw": str(int(r["finish"])) if pd.notna(r["finish"]) else "F",
            "motor_no": p["motor_no"], "boat_no": p["boat_no"], "course": r["course"], "st": r["st"],
            "kimarite": "",
        })
    return pd.DataFrame(rows)


def predict(pdf_path, date, jcd, venue, grade, day_no):
    import history_store as store
    import manshu
    import ml_live
    import ml_model as mm
    import ml_pre
    import pdf_racecard
    from auto_random_fix import _runtime_settings
    from data_paths import data_path
    from pre_picks import build_tickets, risk_race

    programs, racelists, runs = pdf_racecard.parse_pdf(pdf_path, date, jcd, venue, grade=grade, day_no=day_no)
    k = store.read_kind("k_results", "20240927")
    k["racer_id"] = k["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    lw = local_win_rates(k, date, jcd)
    programs["local_win_rate"] = programs["racer_id"].map(lw)

    start = (pd.Timestamp(date) - pd.Timedelta(days=12)).strftime("%Y%m%d")
    recent = k[k["race_date"].astype(str) >= start].copy()
    ev = meet_events(programs, runs, date)
    if len(ev):
        recent = pd.concat([recent, ev.astype({"racer_id": str})], ignore_index=True)
    recent["race_key"] = recent["race_key"].astype(str)
    recent["lane"] = pd.to_numeric(recent["lane"], errors="coerce").astype("Int64")

    version = ml_pre.latest_version()
    model = mm.ChainModel.load(data_path(ml_live.model_file(version)))
    state = ml_live.load_state()
    feats = ml_pre.live_pre_table(programs, racelists, state, recent)
    runtime = _runtime_settings({})
    names = {rk: dict(zip(g["lane"].astype(int), g["racer_name"])) for rk, g in programs.groupby("race_key")}
    out = []
    for rk, f in feats.groupby("race_key", sort=False):
        f = f.reset_index(drop=True)
        final = ml_pre.finals(model, f, names)[rk]
        tickets, hp = build_tickets(final, risk_race(f), runtime)
        p1 = final.set_index("lane")["p_first"].astype(float)
        out.append({"race_key": rk, "race_no": int(rk.split("_")[2]), "hit_probability": float(hp),
                    "favorite_lane": int(p1.idxmax()), "favorite_name": names[rk][int(p1.idxmax())],
                    "favorite_prob": float(p1.max()),
                    "p_first": " ".join(f"{ln}:{p1.get(ln, 0):.0%}" for ln in range(1, 7)),
                    "tickets": " ".join(t["combo"] for t in tickets.to_dict("records"))})
    cands = pd.DataFrame(out)
    probs, longs, mversion = manshu.predict_with_chain("pre", model, feats)
    cands["manshu_prob"] = cands["race_key"].map(probs).astype(float)
    cands["longshot"] = cands["race_key"].map(lambda rk: " ".join(t["combo"] if isinstance(t, dict) else str(t)
                                                                 for t in longs.get(rk, [])))
    cands.attrs.update(version=version, manshu_version=mversion, state_as_of=str(state.get("as_of")))
    return cands.sort_values("race_no"), programs


if __name__ == "__main__":
    date, jcd, venue, grade, day_no, path = sys.argv[1:7]
    c, _ = predict(path, date, jcd, venue, grade, int(day_no))
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 120)
    print(c.attrs)
    print(c.drop(columns=["race_key"]).to_string(index=False))
