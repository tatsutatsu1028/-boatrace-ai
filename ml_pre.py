"""
展示前モデル（展示・直前情報を使わない新しい予想モデル）。「🌅 展示前厳選10」に使う。

今の新モデル（ml_model.ChainModel・ml_features の特徴量）と同じ作りで、当日の朝（1Rの展示より前）に
分かっている情報だけを使う:
  - 番組表（Bファイル）と出走表（勝率・2連率・3連率・平均ST・F/L・級別・締切時刻・節の何日目 など）
  - 前日までの過去の集計（選手・選手×コース・選手×場・モーター・ST）。毎朝の集計表と同じもの
  - 今節成績は前日まで（当日の若いRの結果も使わない）
使わない: 展示タイム・展示ST・展示の進入（想定コースは艇番にする）・チルト・プロペラ/部品交換・
  調整重量・当日体重（直前情報）・気象と水面（直前情報・前のレースの値）・安定板

検証（データ用リポジトリの reports/pre_exhibition/REPORT.md）: テスト3か月で、展示後のモデルより
候補内的中率 −0.5ポイント。「この買い目の的中確率」の高い順に毎日10レース選ぶと候補内的中率 71%。

学習は毎週 ml_nightly.py train が今の新モデルと一緒に行い、models/<版>.joblib と models/pre_latest.json を書く。
毎朝の選定は pre_picks.py。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

import ml_features as mf
import ml_model as mm
from data_paths import data_path

PRE_FAMILY = f"{mm.MODEL_FAMILY}-pre"
PRE_LATEST_FILE = "models/pre_latest.json"

# 展示・直前情報で分かる列（展示前モデルでは使わない）
PRE_DROP = [
    "exhibition_time", "exhibition_st", "tilt", "propeller_new", "parts_exchanged", "adjust_weight",
    "front_entry", "n_front_entry",
    "rk_exh_time", "rk_exh_st", "d_exh_time", "d_exh_st", "in_exh_st", "lane1_exh_st", "st12_ex_diff",
    "wind_speed", "wave_cm", "temperature", "water_temperature", "weather_code", "wind_rel", "wind_head",
    "wind_cross", "stabilizer",
]
# 特徴量を組み立てる前の元の列のうち、展示・直前情報のもの（欠損にしてから組み立てる）
_PRE_BLANK_BASE = [
    "exhibition_time", "p_exhibition_time", "exhibition_course", "exhibition_st", "tilt", "propeller_new",
    "parts_exchanged", "adjust_weight", "p_weight", "temperature", "water_temperature", "p_wind_speed",
    "p_wave_cm", "weather_code", "wind_direction_code", "stadium_direction_code", "stabilizer",
    "pw_wind_speed", "pw_wave_cm", "pw_weather",
]
PRE_FEATURES = [c for c in mf.FEATURES + mm.LANE_FREE_DERIVED if c not in PRE_DROP]
PRE_LANE_FREE = [c for c in mf.LANE_FREE_FEATURES if c not in PRE_DROP]
# 出走表ページ（racelist）から取る列。朝の時点で出ている
RACELIST_COLS = ["grade", "is_final_day", "avg_st", "f_count", "l_count", "national_3ren", "local_3ren",
                 "motor_3ren", "boat_3ren"]


def new_model(version):
    return mm.ChainModel(PRE_FEATURES, version, engine="lgb", lane_free=PRE_LANE_FREE)


def latest_version():
    return json.loads(data_path(PRE_LATEST_FILE).read_text(encoding="utf-8"))["version"]


# ---------------------------------------------------------------
# 特徴量
# ---------------------------------------------------------------
def morning_meet(meet, e):
    """今節成績を「その日の朝」の値にする（その日の最初の走の、前日までの値をその日の全走に使う）。"""
    key = e[["race_key", "lane", "racer_id", "d", "race_no"]].copy()
    key["lane"] = key["lane"].astype(float)
    m = meet.copy()
    m["lane"] = m["lane"].astype(float)
    m = m.merge(key, on=["race_key", "lane"], how="left")
    vals = [c for c in m.columns if c not in ("race_key", "lane", "racer_id", "d", "race_no")]
    # 欠損を飛ばす first() は使わない（その日の2走目の値が1走目に混ざるため）
    m = m.sort_values(["racer_id", "d", "race_no"]).reset_index(drop=True)
    head = m.groupby(["racer_id", "d"], sort=False).cumcount() == 0
    morning = m.loc[head, ["racer_id", "d"] + vals]
    m = m[["race_key", "lane", "racer_id", "d"]].merge(morning, on=["racer_id", "d"], how="left")
    return m[["race_key", "lane"] + vals]


def _blank(base):
    base = base.copy()
    for c in _PRE_BLANK_BASE:
        if c in base.columns:
            base[c] = np.nan
    return base


def build_pre_table(k, b, p, state=None):
    """学習用: ml_features.build_table と同じ行（競走成績のあるレース）の展示前の特徴量。"""
    base = k[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "finish",
              "finish_raw", "exhibition_time", "motor_no", "distance_m", "fixed_entry",
              "race_type", "day_no"]].copy()
    base = base[~base["finish_raw"].astype(str).str.strip().str.startswith("K")].copy()
    base["finish"] = mf._num(base["finish"])
    base = base.merge(mf._program_times(b), on="race_key", how="left")
    base = base.merge(b[["race_key", "lane"] + mf._B_COLS].drop_duplicates(["race_key", "lane"]),
                      on=["race_key", "lane"], how="left")
    pp = p[mf._PAGES_COLS].drop_duplicates(["race_key", "lane"]).rename(columns=mf._PAGES_RENAME)
    base = base.merge(pp, on=["race_key", "lane"], how="left")
    e = mf._events(k)
    meet = morning_meet(mf.meet_features(e), e)
    return mf.assemble(_blank(base), state if state is not None else mf.build_state(e), meet)


def _today_events(programs):
    """当日の番組表の行を、結果の無い「走」として今節の集計に足す（今節の何日目かを揃えるため）。"""
    t = programs[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "motor_no", "day_no",
                  "meet_title"]].copy()
    for c in ("finish", "course", "st"):
        t[c] = np.nan
    t["finish_raw"] = ""
    t["kimarite"] = ""
    return t


def live_pre_table(programs, racelists, state, k_recent):
    """
    本番（朝）用: その日の番組表（official_download.parse_b の形）、出走表ページの値（race_key・lane と
    RACELIST_COLS。取れなかったレースは無くてよい）、前日までの集計表（ml_state）、直近の競走成績
    （今節成績の計算用。前日まで）から、学習と同じ展示前の特徴量を作る。
    """
    b = mf.unify_strings(programs.copy())
    b["race_key"] = b["race_key"].astype(str)
    b["race_date"] = b["race_date"].astype(str)
    b["jcd"] = b["jcd"].astype(str).str.zfill(2)
    b["lane"] = mf._num(b["lane"]).astype("Int64")
    b["racer_id"] = b["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    b = b.drop_duplicates(["race_key", "lane"])
    base = b[["race_key", "race_date", "jcd", "race_no", "lane", "racer_id", "motor_no", "distance_m",
              "fixed_entry", "race_type", "day_no"] + mf._B_COLS].copy()
    base["finish"] = np.nan
    base = base.merge(mf._program_times(b), on="race_key", how="left")
    rl = racelists.copy() if racelists is not None and len(racelists) else pd.DataFrame(columns=["race_key", "lane"])
    rl = mf.unify_strings(rl)
    rl["race_key"] = rl["race_key"].astype(str)
    rl["lane"] = mf._num(rl["lane"]).astype("Int64")
    for c in mf._PAGES_COLS:
        if c not in rl.columns:
            rl[c] = np.nan
    pp = rl[mf._PAGES_COLS].drop_duplicates(["race_key", "lane"]).rename(columns=mf._PAGES_RENAME)
    base = base.merge(pp, on=["race_key", "lane"], how="left")

    k = mf.unify_strings(k_recent.copy())
    k["race_key"] = k["race_key"].astype(str)
    k["lane"] = mf._num(k["lane"]).astype("Int64")
    k["racer_id"] = k["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    day = str(b["race_date"].iloc[0])
    k = k[k["race_date"].astype(str) < day]
    k = pd.concat([k, _today_events(b)], ignore_index=True)
    e = mf._events(k)
    meet = morning_meet(mf.meet_features(e), e)
    meet = meet[meet["race_key"].isin(set(base["race_key"]))]
    return mf.assemble(mf.unify_strings(_blank(base)), mf.unify_state(state), mf.unify_strings(meet))


def finals(model, feats, names=None):
    """全レース分の final（prediction.predict と同じ形）を {race_key: final} で返す。"""
    out = {}
    for rk, f in feats.groupby("race_key", sort=False):
        fin = mm.to_final(model, f.reset_index(drop=True))
        if names is not None:
            fin["racer_name"] = fin["lane"].map(names.get(rk, {}))
        out[rk] = fin
    return out
