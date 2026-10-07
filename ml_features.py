"""
新しい予想モデル（ml_model.py）の特徴量を、過去データ（data/history/）から作る。

予想に使うのは「そのレースの時点で分かっていた情報」だけ:
  - 番組表（b_programs）・出走表と直前情報（pages）の値はレース前に出ているのでそのまま使う
  - 競走成績（k_results）の展示タイムもレース前に出ている値なので使う
  - 進入コース・ST・着順・決まり手はレースの結果なので、そのレース自身の値は使わず、
    「それより前のレース」の集計（選手の調子・選手×コース・選手×場・モーター・今節）にだけ使う
  - 気象は、直前情報（前のレース時点の値）が無い期間は、同じ場・同じ日の1つ前のレースの
    競走成績の値で代わりにする（どちらも「そのレースの前に分かっていた値」）
  - 今節成績は、同じ節のそのレースより前に終わったレースだけ（前日まで＋当日の若いR）

列の分類（ドキュメントの「学習に使う特徴量」の9分類に対応）は FEATURE_GROUPS を参照。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import history_store as store

# 進入コース別の平均着順（今節成績の「コースを考えた着順」の基準）。
# course_baseline.py / hindcast.py と同じ値。
_COURSE_BASELINE = {1: 1.92, 2: 2.88, 3: 2.99, 4: 3.18, 5: 3.42, 6: 3.61}
_CLASS_ORD = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}
_GRADE_ORD = {"SG": 5, "G1": 4, "G2": 3, "G3": 2, "一般": 1}
_RACE_TYPE_ORD = {"一般": 0, "予選": 1, "その他": 1, "特別選抜": 2, "準優勝戦": 3, "優勝戦": 4}
_WEATHER = {"晴": 1, "曇り": 2, "雨": 3, "雪": 4, "霧": 5}

FEATURE_GROUPS = {
    "選手の実力": [
        "class_ord", "national_win_rate", "national_2ren", "national_3ren", "age", "weight",
        "r365_n", "r365_win", "r365_top2", "r365_top3", "r365_fin",
        "r90_n", "r90_win", "r90_top3", "r90_fin", "last5_fin", "last10_fin",
    ],
    "選手×コース": [
        "exp_course", "front_entry", "rc_n", "rc_win", "rc_top2", "rc_top3", "rc_st",
        "racer_avg_course_shift",
    ],
    "選手×場": ["local_win_rate", "local_2ren", "local_3ren", "rv_n", "rv_win", "rv_top3"],
    "スタート": [
        "avg_st", "r180_st", "r180_st_sd", "r180_st_late", "exhibition_st",
        "f_count", "l_count", "f_period", "f_flag",
    ],
    "今節": ["meet_n", "meet_fin", "meet_fin_adj", "meet_top2", "meet_st", "day_no", "is_final_day"],
    "機材": [
        "motor_2ren", "motor_3ren", "boat_2ren", "boat_3ren", "mot_n", "mot_top2", "mot_fin",
        "exhibition_time", "tilt", "propeller_new", "parts_exchanged", "adjust_weight",
    ],
    "気象・水面": [
        "wind_speed", "wave_cm", "temperature", "water_temperature", "weather_code",
        "wind_rel", "wind_head", "wind_cross",
    ],
    "レース条件": [
        "jcd", "race_no", "lane", "distance_m", "fixed_entry", "race_type_ord", "grade_ord",
        "stabilizer",
    ],
    "艇同士の関係": [
        "rk_win_rate", "rk_r365_win", "rk_exh_time", "rk_exh_st", "rk_avg_st", "rk_motor",
        "rk_rc_win", "d_win_rate", "d_exh_time", "d_exh_st", "d_avg_st", "d_rc_win",
        "in_win_rate", "in_avg_st", "in_exh_st", "out_win_rate", "out_avg_st",
        "lane1_win_rate", "lane1_rc_win", "lane1_exh_st", "n_boats", "n_front_entry",
    ],
}
CAT_FEATURES = ["jcd", "wind_rel"]
FEATURES = [c for cols in FEATURE_GROUPS.values() for c in cols]
# 出走表・直前情報（pages）にしか無い列。2年分で学習するときは pages の無い期間は欠損になる。
PAGES_ONLY = [
    "national_3ren", "local_3ren", "motor_3ren", "boat_3ren", "avg_st", "f_count", "l_count",
    "exhibition_st", "exhibition_course", "tilt", "propeller_new", "parts_exchanged",
    "adjust_weight", "temperature", "water_temperature", "weather_code", "wind_rel",
    "wind_head", "wind_cross", "grade_ord", "stabilizer", "is_final_day",
]


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _race_type(name):
    s = str(name or "")
    if "準優" in s:
        return "準優勝戦"
    if "優勝" in s:
        return "優勝戦"
    if any(w in s for w in ("選抜", "特選", "ドリーム", "特賞", "特別")) and "予選" not in s:
        return "特別選抜"
    if "予選" in s:
        return "予選"
    if "一般" in s:
        return "一般"
    return "その他"


# ---------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------
def load_sources(start=None, end=None):
    """競走成績・番組表・出走表/直前情報を読む（race_key の文字列で揃える）。"""
    k = store.read_kind("k_results", start, end)
    b = store.read_kind("b_programs", start, end)
    p = store.read_kind("pages", start, end)
    for df in (k, b, p):
        df["race_key"] = df["race_key"].astype(str)
        df["lane"] = _num(df["lane"]).astype("Int64")
        if "racer_id" in df.columns:
            df["racer_id"] = df["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    return k, b, p


# ---------------------------------------------------------------
# 過去の結果の集計（そのレースより前だけ）
# ---------------------------------------------------------------
def _events(k):
    """競走成績を「1走1行」の集計用の形にする。欠場（K）は走っていないので除く。"""
    e = k[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "finish", "finish_raw",
           "course", "st", "motor_no", "day_no", "meet_title"]].copy()
    raw = e["finish_raw"].astype(str).str.strip()
    e = e[~raw.str.startswith("K")].copy()
    raw = e["finish_raw"].astype(str).str.strip()
    e["d"] = pd.to_datetime(e["race_date"].astype(str), format="%Y%m%d")
    e["race_no"] = _num(e["race_no"])
    e["course"] = _num(e["course"])
    e["st"] = _num(e["st"])
    fin = _num(e["finish"])
    e["fin6"] = fin.fillna(6.0)  # 失格・F・L・転覆などは6着扱い
    e["win"] = (fin == 1).astype(float)
    e["top2"] = (fin <= 2).astype(float)
    e["top3"] = (fin <= 3).astype(float)
    e["is_f"] = raw.eq("F").astype(float)
    e["st_ok"] = e["st"].where(e["st"] > 0)
    e["st_late"] = (e["st_ok"] >= 0.20).astype(float).where(e["st_ok"].notna())
    e["one"] = 1.0
    e["course_shift"] = (e["lane"].astype(float) - e["course"])
    return e


def _cum_by_day(e, keys, cols):
    """keys×日ごとの合計の累積（その日を含む）。"""
    g = e.groupby(keys + ["d"], sort=False)[cols].sum().reset_index()
    g = g.sort_values(keys + ["d"])
    g[cols] = g.groupby(keys, sort=False)[cols].cumsum()
    return g


def _asof(targets, cum, keys, cols, shift_days=0, suffix=""):
    """targets の各行の「d - shift_days より前の日まで」の累積値。"""
    t = targets[keys + ["d"]].copy()
    t["_row"] = np.arange(len(t))
    t["_q"] = t["d"] - pd.Timedelta(days=shift_days)
    c = cum.rename(columns={"d": "_q"})
    t = t.sort_values("_q")
    c = c.sort_values("_q")
    m = pd.merge_asof(t, c, on="_q", by=keys, allow_exact_matches=False)
    m = m.sort_values("_row")
    return m[cols].fillna(0.0).to_numpy()


def _window(targets, cum, keys, cols, days):
    """d より前の days 日間の合計（当日は含まない）。"""
    now = _asof(targets, cum, keys, cols, 0)
    if days is None:
        return now
    old = _asof(targets, cum, keys, cols, days)
    return now - old


def _rates(prefix, sums, cols, min_n=1):
    n = sums[:, cols.index("one")]
    out = {f"{prefix}_n": n}
    safe = np.where(n >= min_n, n, np.nan)
    for c, name in (("win", "win"), ("top2", "top2"), ("top3", "top3"), ("fin6", "fin")):
        if c in cols:
            out[f"{prefix}_{name}"] = sums[:, cols.index(c)] / safe
    return out


def build_state(e, keep_days=None):
    """
    過去の結果の集計表（累積）。history_features はこれだけを見て計算する。
    学習では全期間の e から作り、本番では毎晩「昨日まで」の e から作って保存しておけば、
    アプリは過去データ全体を読まずに同じ値を出せる（keep_days で古い日付を落として小さくする）。
    """
    cols = ["one", "win", "top2", "top3", "fin6"]
    e2 = e.assign(st_n=e["st_ok"].notna().astype(float), st_sum=e["st_ok"].fillna(0.0),
                  st_sq=e["st_ok"].fillna(0.0) ** 2, late=e["st_late"].fillna(0.0),
                  shift_n=e["course_shift"].notna().astype(float),
                  shift_sum=e["course_shift"].fillna(0.0))
    ec = e[e["course"].between(1, 6)].assign(ckey=lambda x: x["course"].astype(int).astype(str))
    ec = ec.assign(st_n=ec["st_ok"].notna().astype(float), st_sum=ec["st_ok"].fillna(0.0))
    em = e.assign(mkey=e["jcd"].astype(str) + "_" + _num(e["motor_no"]).astype("Int64").astype(str))
    state = {
        "racer": _cum_by_day(e, ["racer_id"], cols),
        "start": _cum_by_day(e2, ["racer_id"], ["st_n", "st_sum", "st_sq", "late", "shift_n", "shift_sum"]),
        "f": _cum_by_day(e, ["racer_id"], ["is_f"]),
        "course": _cum_by_day(ec, ["racer_id", "ckey"], ["one", "win", "top2", "top3", "st_n", "st_sum"]),
        "venue": _cum_by_day(e, ["racer_id", "jcd"], cols),
        "motor": _cum_by_day(em, ["mkey"], cols),
    }
    seq = e.sort_values(["racer_id", "d", "race_no"])[["racer_id", "d", "fin6"]].copy()
    g = seq.groupby("racer_id", sort=False)["fin6"]
    seq["last5_fin"] = g.transform(lambda x: x.rolling(5, min_periods=3).mean())
    seq["last10_fin"] = g.transform(lambda x: x.rolling(10, min_periods=5).mean())
    state["last"] = seq.groupby(["racer_id", "d"], sort=False)[["last5_fin", "last10_fin"]].last().reset_index()
    if keep_days:
        state = trim_state(state, e["d"].max() + pd.Timedelta(days=1), keep_days)
    return state


def trim_state(state, as_of, keep_days):
    """as_of の予想に要る分だけ残す（累積は「窓の始まりより前の最後の行」と「それ以降」）。"""
    lo = pd.Timestamp(as_of) - pd.Timedelta(days=keep_days)
    out = {}
    for name, t in state.items():
        if name == "last":
            out[name] = t.sort_values("d").groupby("racer_id").tail(1)
            continue
        keys = [c for c in t.columns if c in ("racer_id", "ckey", "jcd", "mkey")]
        before = t[t["d"] < lo].sort_values("d").groupby(keys).tail(1)
        out[name] = pd.concat([before, t[t["d"] >= lo]]).sort_values(keys + ["d"])
    return out


_WINDOW_DAYS = (365, 180, 90)


def _period_start(d):
    d = pd.Timestamp(d)
    if d.month >= 11:
        return pd.Timestamp(d.year, 11, 1)
    if d.month >= 5:
        return pd.Timestamp(d.year, 5, 1)
    return pd.Timestamp(d.year - 1, 11, 1)


def compact_state(state, as_of):
    """
    as_of の日のレースの予想に要る行だけ残す（アプリに毎日渡す小さな集計表）。
    history_features が引く日付（as_of と、その 365/180/90 日前・級別審査期間の初日）の
    直前の累積だけあれば、merge_asof の結果は元の表と同じになる。
    """
    as_of = pd.Timestamp(as_of)
    qs = [as_of] + [as_of - pd.Timedelta(days=w) for w in _WINDOW_DAYS] + [_period_start(as_of)]
    out = {"as_of": as_of}
    for name, t in state.items():
        if not isinstance(t, pd.DataFrame):
            continue
        keys = [c for c in t.columns if c in ("racer_id", "ckey", "jcd", "mkey")]
        parts = [t[t["d"] < q].sort_values("d").groupby(keys, sort=False).tail(1) for q in qs]
        out[name] = pd.concat(parts).drop_duplicates(keys + ["d"]).sort_values(keys + ["d"]).reset_index(drop=True)
    return out


def history_features(base, state):
    """選手の調子・選手×コース・選手×場・スタート・モーター・F の集計（そのレースの前日まで）。"""
    if isinstance(state, pd.DataFrame):  # 事象の表（_events）を直接渡したとき
        state = build_state(state)
    base = base.copy()
    base["d"] = pd.to_datetime(base["race_date"].astype(str), format="%Y%m%d")
    cols = ["one", "win", "top2", "top3", "fin6"]
    out = {}

    for days, pre in ((365, "r365"), (90, "r90")):
        out.update(_rates(pre, _window(base, state["racer"], ["racer_id"], cols, days), cols))

    # スタート（STはフライング・出遅れを除いた正の値だけ）
    scols = ["st_n", "st_sum", "st_sq", "late", "shift_n", "shift_sum"]
    s = _window(base, state["start"], ["racer_id"], scols, 180)
    n = np.where(s[:, 0] >= 3, s[:, 0], np.nan)
    out["r180_st"] = s[:, 1] / n
    out["r180_st_sd"] = np.sqrt(np.clip(s[:, 2] / n - (s[:, 1] / n) ** 2, 0, None))
    out["r180_st_late"] = s[:, 3] / n
    sn = np.where(s[:, 4] >= 3, s[:, 4], np.nan)
    out["racer_avg_course_shift"] = s[:, 5] / sn

    # フライング: 今の級別審査期間（5/1〜・11/1〜）に入ってからの F 回数
    now = _asof(base, state["f"], ["racer_id"], ["is_f"], 0)[:, 0]
    d = base["d"]
    period_start = pd.to_datetime(np.where(
        d.dt.month >= 11, d.dt.year.astype(str) + "-11-01",
        np.where(d.dt.month >= 5, d.dt.year.astype(str) + "-05-01",
                 (d.dt.year - 1).astype(str) + "-11-01")))
    tmp = base[["racer_id"]].copy()
    tmp["d"] = period_start
    old = _asof(tmp, state["f"], ["racer_id"], ["is_f"], 0)[:, 0]
    out["f_period"] = now - old

    # 選手×コース（予想時点の想定コース = 展示の進入コース、無ければ艇番）
    ecols = ["one", "win", "top2", "top3", "st_n", "st_sum"]
    t = base.assign(ckey=base["exp_course"].astype(int).astype(str))
    s = _window(t, state["course"], ["racer_id", "ckey"], ecols, 365)
    out.update(_rates("rc", s, ecols, min_n=1))
    stn = np.where(s[:, ecols.index("st_n")] >= 2, s[:, ecols.index("st_n")], np.nan)
    out["rc_st"] = s[:, ecols.index("st_sum")] / stn

    # 選手×場
    r = _rates("rv", _window(base, state["venue"], ["racer_id", "jcd"], cols, 365), cols)
    out.update({k: v for k, v in r.items() if k in ("rv_n", "rv_win", "rv_top3")})

    # モーター（同じ場・同じ番号の直近90日。モーターの入れ替えは年1回なのでほぼ同じ機体）
    t = base.assign(mkey=base["jcd"].astype(str) + "_" + _num(base["motor_no"]).astype("Int64").astype(str))
    r = _rates("mot", _window(t, state["motor"], ["mkey"], cols, 90), cols)
    out.update({"mot_n": r["mot_n"], "mot_top2": r["mot_top2"], "mot_fin": r["mot_fin"]})

    res = pd.DataFrame(out, index=base.index)

    # 直近5走・10走の平均着順（前日まで）
    t = base[["racer_id", "d"]].copy()
    t["_row"] = np.arange(len(t))
    m = pd.merge_asof(t.sort_values("d"), state["last"].sort_values("d"), on="d", by="racer_id",
                      allow_exact_matches=False).sort_values("_row")
    res["last5_fin"] = m["last5_fin"].to_numpy()
    res["last10_fin"] = m["last10_fin"].to_numpy()
    return res


def meet_features(e):
    """今節成績: 同じ節の、そのレースより前に終わったレース（前日まで＋当日の若いR）。"""
    x = e.copy()
    day_no = _num(x["day_no"]).fillna(1)
    x["meet_start"] = x["d"] - pd.to_timedelta(day_no - 1, unit="D")
    x["meet_id"] = x["jcd"].astype(str) + "_" + x["meet_start"].dt.strftime("%Y%m%d")
    x = x.sort_values(["meet_id", "racer_id", "d", "race_no"])
    # 本番の今節成績（current_meet_fetcher）と同じ定義:
    #   走数 = 進入コースとSTが出ている走、着順・2連対率(%)は1〜6着の走だけ、STはその平均
    fin = _num(x["finish"])
    valid = x["course"].between(1, 6) & x["st"].notna()
    placed = valid & fin.between(1, 6)
    adj = (fin - x["course"].map(_COURSE_BASELINE)).where(placed)
    x["n_run"] = valid.astype(float)
    x["st_sum"] = x["st"].where(valid).fillna(0.0)
    x["fin_n"] = placed.astype(float)
    x["fin_sum"] = fin.where(placed).fillna(0.0)
    x["top2_sum"] = (fin <= 2).where(placed).fillna(0.0).astype(float)
    x["adj_n"] = adj.notna().astype(float)
    x["adj_sum"] = adj.fillna(0.0)
    cols = ["n_run", "st_sum", "fin_n", "fin_sum", "top2_sum", "adj_n", "adj_sum"]
    g = x.groupby(["meet_id", "racer_id"], sort=False)[cols]
    prev = g.cumsum() - x[cols]
    fn = prev["fin_n"].where(prev["fin_n"] > 0)
    out = pd.DataFrame({
        "race_key": x["race_key"], "lane": x["lane"],
        "meet_n": prev["n_run"],
        "meet_fin": prev["fin_sum"] / fn,
        "meet_top2": prev["top2_sum"] / fn * 100.0,
        "meet_st": prev["st_sum"] / prev["n_run"].where(prev["n_run"] > 0),
        "meet_fin_adj": prev["adj_sum"] / prev["adj_n"].where(prev["adj_n"] > 0),
    })
    return out


def prev_race_weather(k):
    """同じ場・同じ日の1つ前のレースの気象（競走成績の値）。直前情報が無い期間の代わり。"""
    r = k.drop_duplicates("race_key")[["race_key", "race_date", "jcd", "race_no", "wind_speed",
                                       "wave_cm", "weather"]].copy()
    r["race_no"] = _num(r["race_no"])
    r = r.sort_values(["race_date", "jcd", "race_no"])
    g = r.groupby(["race_date", "jcd"], sort=False)
    return pd.DataFrame({
        "race_key": r["race_key"],
        "pw_wind_speed": g["wind_speed"].shift(1).pipe(_num),
        "pw_wave_cm": g["wave_cm"].shift(1).pipe(_num),
        "pw_weather": g["weather"].shift(1).map(_WEATHER),
    })


# ---------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------
def build_table(k, b, p, start=None, end=None):
    """
    1レース1艇1行の表（特徴量＋着順）。対象は競走成績のあるレース（start〜end）。
    k は start より前の集計にも使うので、なるべく長い期間を渡す。
    """
    base = k[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "finish",
              "finish_raw", "exhibition_time", "motor_no", "distance_m", "fixed_entry",
              "race_type", "day_no"]].copy()
    if start:
        base = base[base["race_date"].astype(str) >= str(start)]
    if end:
        base = base[base["race_date"].astype(str) <= str(end)]
    base = base[~base["finish_raw"].astype(str).str.strip().str.startswith("K")].copy()
    base["finish"] = _num(base["finish"])
    base["exhibition_time"] = _num(base["exhibition_time"]).where(lambda s: s.between(6.0, 8.0))

    base = base.merge(b[["race_key", "lane"] + _B_COLS].drop_duplicates(["race_key", "lane"]), on=["race_key", "lane"], how="left")

    pp = p[_PAGES_COLS].drop_duplicates(["race_key", "lane"]).rename(columns=_PAGES_RENAME)
    base = base.merge(pp, on=["race_key", "lane"], how="left")
    base = base.merge(prev_race_weather(k), on="race_key", how="left")
    e = _events(k)
    return assemble(base, build_state(e), meet_features(e))


_PAGES_COLS = ["race_key", "lane", "grade", "is_final_day", "stabilizer", "temperature",
               "water_temperature", "wind_speed", "wave_cm", "weather_code", "wind_direction_code",
               "stadium_direction_code", "avg_st", "f_count", "l_count", "national_3ren", "local_3ren",
               "motor_3ren", "boat_3ren", "weight", "tilt", "propeller_new", "parts_exchanged",
               "adjust_weight", "exhibition_course", "exhibition_st", "exhibition_time"]
_PAGES_RENAME = {"weight": "p_weight", "wind_speed": "p_wind_speed", "wave_cm": "p_wave_cm",
                 "exhibition_time": "p_exhibition_time"}


def assemble(base, state, meet):
    """
    base（1レース1艇1行。番組表・出走表/直前情報・競走成績の展示タイム・1つ前のレースの気象を
    並べたもの）に、過去の集計（state）と今節成績（meet）を足して特徴量の表にする。
    学習（build_table）と本番（live_table）で同じ処理を通す。
    """
    base = base.reset_index(drop=True)
    for c in ("pw_wind_speed", "pw_wave_cm", "pw_weather", "grade", "avg_st"):
        if c not in base.columns:
            base[c] = np.nan
    base["has_pages"] = base["grade"].notna() | base["avg_st"].notna()
    num = lambda c: _num(base[c]) if c in base.columns else pd.Series(np.nan, index=base.index)  # noqa: E731
    f = pd.DataFrame(index=base.index)
    f["race_key"] = base["race_key"]
    f["race_date"] = base["race_date"].astype(str)
    f["racer_id"] = base["racer_id"]
    f["lane"] = num("lane").astype(float)
    f["finish"] = num("finish")
    f["jcd"] = base["jcd"].astype(str).str.zfill(2)
    f["race_no"] = num("race_no")
    f["distance_m"] = num("distance_m")
    f["fixed_entry"] = num("fixed_entry").fillna(0)
    f["race_type_ord"] = base["race_type"].map(_race_type).map(_RACE_TYPE_ORD)
    f["grade_ord"] = base["grade"].map(_GRADE_ORD)
    f["stabilizer"] = num("stabilizer")
    f["is_final_day"] = num("is_final_day")
    f["day_no"] = num("day_no")

    f["class_ord"] = base["racer_class"].map(_CLASS_ORD)
    for c in ("national_win_rate", "national_2ren", "local_win_rate", "local_2ren", "motor_2ren",
              "boat_2ren", "age", "national_3ren", "local_3ren", "motor_3ren", "boat_3ren", "avg_st",
              "f_count", "l_count", "tilt", "propeller_new", "parts_exchanged", "adjust_weight",
              "exhibition_st", "temperature", "water_temperature", "weather_code"):
        f[c] = num(c)
    f["weight"] = num("p_weight").fillna(num("weight"))
    f["exhibition_time"] = num("p_exhibition_time").where(lambda s: s.between(6.0, 8.0)).fillna(
        num("exhibition_time").where(lambda s: s.between(6.0, 8.0)))
    f["avg_st"] = f["avg_st"].where(f["avg_st"] > 0)
    f["exhibition_st"] = f["exhibition_st"].where(f["exhibition_st"].abs() < 1.0)

    # 気象: 直前情報（前のレース時点）を優先、無ければ1つ前のレースの競走成績
    f["wind_speed"] = num("p_wind_speed").fillna(num("pw_wind_speed"))
    f["wave_cm"] = num("p_wave_cm").fillna(num("pw_wave_cm"))
    f["weather_code"] = f["weather_code"].fillna(num("pw_weather"))
    wc, sc = num("wind_direction_code"), num("stadium_direction_code")
    rel = ((wc - sc) % 16).where(wc.between(1, 16) & sc.between(1, 16))
    f["wind_rel"] = rel.where(f["wind_speed"] > 0, -1).where(wc.notna() | (f["wind_speed"] == 0))
    ang = rel * (2 * np.pi / 16)
    f["wind_head"] = (np.cos(ang) * f["wind_speed"]).where(rel.notna())
    f["wind_cross"] = (np.sin(ang) * f["wind_speed"]).where(rel.notna())

    ex_course = num("exhibition_course").where(lambda s: s.between(1, 6))
    f["exp_course"] = ex_course.fillna(f["lane"])
    f["front_entry"] = (f["lane"] - f["exp_course"]).where(ex_course.notna())
    f["motor_no"] = num("motor_no")

    hist = history_features(f, state)
    f = pd.concat([f, hist], axis=1)
    f = _merge_meet(f, meet)

    f["f_flag"] = (f["f_count"].fillna(f["f_period"]) > 0).astype(float)
    f["has_pages"] = base["has_pages"].to_numpy()
    return add_relations(f)


def _merge_meet(f, meet):
    meet = meet.copy()
    meet["lane"] = meet["lane"].astype(float)
    return f.merge(meet.drop_duplicates(["race_key", "lane"]), on=["race_key", "lane"], how="left")


def add_relations(f):
    """同じレースの艇同士の比較（順位・平均との差・隣の艇・1号艇）。"""
    f = f.sort_values(["race_key", "lane"]).reset_index(drop=True)
    g = f.groupby("race_key", sort=False)

    def rank(col, ascending):
        return g[col].rank(ascending=ascending, method="average")

    f["rk_win_rate"] = rank("national_win_rate", False)
    f["rk_r365_win"] = rank("r365_win", False)
    f["rk_exh_time"] = rank("exhibition_time", True)
    f["rk_exh_st"] = rank("exhibition_st", True)
    f["rk_avg_st"] = rank("avg_st", True) if f["avg_st"].notna().any() else np.nan
    f["rk_avg_st"] = f["rk_avg_st"].fillna(rank("r180_st", True))
    f["rk_motor"] = rank("motor_2ren", False)
    f["rk_rc_win"] = rank("rc_win", False)
    for col, name in (("national_win_rate", "d_win_rate"), ("exhibition_time", "d_exh_time"),
                      ("exhibition_st", "d_exh_st"), ("rc_win", "d_rc_win")):
        f[name] = f[col] - g[col].transform("mean")
    st = f["avg_st"].fillna(f["r180_st"])
    f["d_avg_st"] = st - st.groupby(f["race_key"]).transform("mean")

    # 隣の艇（想定コースで並べた内側・外側）
    order = f.sort_values(["race_key", "exp_course", "lane"])
    og = order.groupby("race_key", sort=False)
    st_o = order["avg_st"].fillna(order["r180_st"])
    inner = pd.DataFrame({
        "in_win_rate": og["national_win_rate"].shift(1),
        "in_avg_st": st_o.groupby(order["race_key"]).shift(1),
        "in_exh_st": og["exhibition_st"].shift(1),
        "out_win_rate": og["national_win_rate"].shift(-1),
        "out_avg_st": st_o.groupby(order["race_key"]).shift(-1),
    }, index=order.index)
    f = f.join(inner)

    lane1 = f[f["lane"] == 1].set_index("race_key")
    f["lane1_win_rate"] = f["race_key"].map(lane1["national_win_rate"])
    f["lane1_rc_win"] = f["race_key"].map(lane1["rc_win"])
    f["lane1_exh_st"] = f["race_key"].map(lane1["exhibition_st"])
    f["n_boats"] = g["lane"].transform("count")
    f["n_front_entry"] = (f["front_entry"] > 0).groupby(f["race_key"]).transform("sum").where(
        f["front_entry"].notna())
    return f


_LIVE_MEET = {
    "current_meet_races": "meet_n", "current_meet_avg_finish": "meet_fin",
    "current_meet_top2_rate": "meet_top2", "current_meet_avg_st": "meet_st",
    "current_meet_avg_finish_adjusted": "meet_fin_adj",
}


_B_COLS = ["age", "weight", "racer_class", "national_win_rate", "national_2ren", "local_win_rate",
           "local_2ren", "motor_2ren", "boat_2ren"]


def live_table(pages, programs, state, current_meet=None):
    """
    本番の予想用: 1レース分の出走表・直前情報（history_pages.collect_race と同じ形）、
    その日の番組表（official_download.parse_b と同じ形）、前日までの集計（build_state を
    毎晩保存したもの）から、学習と同じ特徴量を作る。

    勝率・2連率などは学習と同じく番組表（Bファイル）の値を使う（出走表ページの当地勝率は
    番組表と食い違うことがあるため）。今節成績は本番の current_meet_fetcher の値
    （同じ定義で学習している）をそのまま使う。
    """
    p = pages.copy()
    p["race_key"] = p["race_key"].astype(str)
    p["lane"] = _num(p["lane"])
    p["racer_id"] = p["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    for c in _PAGES_COLS + ["race_name", "motor_no", "distance_m", "fixed_entry", "day_no"]:
        if c not in p.columns:
            p[c] = np.nan
    base = p[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "motor_no", "distance_m",
              "fixed_entry", "day_no"]].copy()
    base["race_type"] = p["race_name"]
    base["finish"] = np.nan
    b = programs.copy() if programs is not None else pd.DataFrame(columns=["race_key", "lane"])
    b["race_key"] = b["race_key"].astype(str)
    b["lane"] = _num(b["lane"])
    for c in _B_COLS:
        if c not in b.columns:
            b[c] = np.nan
    if "race_type" not in b.columns:
        b["race_type"] = np.nan
    base = base.merge(b[["race_key", "lane"] + _B_COLS + ["race_type"]].drop_duplicates(["race_key", "lane"])
                      .rename(columns={"race_type": "b_race_type"}), on=["race_key", "lane"], how="left")
    # レース名は学習と同じく番組表の表記を使う（無ければ出走表ページの表記）
    base["race_type"] = base["b_race_type"].fillna(base["race_type"])
    base = base.merge(p[_PAGES_COLS].rename(columns=_PAGES_RENAME), on=["race_key", "lane"], how="left")
    meet = pd.DataFrame({"race_key": base["race_key"], "lane": base["lane"]})
    if current_meet is not None and len(current_meet):
        cm = current_meet.rename(columns=_LIVE_MEET)
        cm["lane"] = _num(cm["lane"])
        meet = meet.merge(cm[["lane"] + [c for c in _LIVE_MEET.values() if c in cm.columns]],
                          on="lane", how="left")
    for c in _LIVE_MEET.values():
        if c not in meet.columns:
            meet[c] = np.nan
    meet["meet_n"] = meet["meet_n"].fillna(0)
    return assemble(base, state, meet)


def build_dataset(start, end, history_start=None):
    """start〜end のレースの表。集計用の競走成績は history_start（既定は全期間）から読む。"""
    k, b, p = load_sources(history_start, end)
    return build_table(k, b, p, start, end)
