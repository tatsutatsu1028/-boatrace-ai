"""
全レースの事後予想（hindcast）。

毎晩の日次収集（collect_history_workflow.yml）の後に、前日の全レースを
今の本番の予想ロジックで予想し直し、実際の結果と突き合わせて Supabase に保存する。
起動は pg_cron → hindcast.yml（workflow_dispatch）。

その日の時点で分かっていた情報だけを使う:
  - 出走表・直前情報（級別・勝率・モーター/ボート2連率・平均ST・体重・展示・チルト・
    気象）は history_full.csv の値（本番と同じ official_fetcher で取得したもの。
    気象は直前情報ページの「前のレース時点」の値）
  - 今節成績は公式の競走成績（k_results）から、同じ節のそのレースより前に
    終わったレースだけで計算する（前日まで＋当日の若いレース番号）
  - 選手のコース別成績は、本番が読む公式の選手ページと同じ集計期間
    （級別審査期間: 1〜6月のレースは前年5/1〜10/31、7〜12月は前年11/1〜4/30）で計算する
  - 場のコース別入着率・決まり手は、前月までの3か月分で計算する
  - 1着モデルは本番と同じ sample_history.csv（その日より前の行だけ）で、
    2着・3着モデルと決まり手プロファイルは history_full.csv のその日より前の行で学習する
1着モデルの学習データ（sample_history.csv）は 2026-08-17 までなので、
それと重ならない 2026-08-18 以降を対象にする（--start で変えられる）。

保存先（supabase/migrations/20261006000000_create_hindcast_tables.sql）:
  hindcast_predictions  1レース1行: 1着確率・本命・買い目候補・実際の3連単・的中・モデルの版
  hindcast_inputs       1艇1行: 予想に使った入力（結果の列は入れない）
  hindcast_runs         1日1行: 予想の設定・学習データの範囲（その日の保存完了の印も兼ねる）

対象日は --start〜--end のうち、この版の予想がまだ保存されていない日。
直近の競走成績ファイル（K）がまだ公開されていない日は飛ばし、次の晩に回す。

使い方:
  python hindcast.py                         # 2026-08-18〜昨日の未保存分
  python hindcast.py --start 20261001 --end 20261003 --dry-run out/
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import history_store as store
from data_paths import data_path
from official_fetcher import VENUES
from prediction import (
    adaptive_ticket_plan,
    assess_favorite_risk,
    confidence,
    predict,
    rank_tickets,
    second_favorite_n_for,
    train,
    trifecta,
)
from hit_calibration import apply_calibration, latest_from_env, prediction_version
from stake_allocator import ticket_hit_probability

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_START = "20260818"
# 予想ロジックの版。1着〜3着モデル・買い目方針のどれかが変わればこの文字列も変わる。
HINDCAST_MODEL_VERSION = prediction_version()
INPUT_VERSION = "hindcast-inputs-v1"

PREDICTION_TABLE = "hindcast_predictions"
INPUT_TABLE = "hindcast_inputs"
RUN_TABLE = "hindcast_runs"

# 今節成績の「コース基準着順」が history_full.csv から計算できない場合の値
# （course_baseline.py と同じ）。
_FALLBACK_BASELINE = {1: 1.92, 2: 2.88, 3: 2.99, 4: 3.18, 5: 3.42, 6: 3.61}

_VENUE_FINISH_COLS = {
    1: "venue_course_1st", 2: "venue_course_2nd", 3: "venue_course_3rd",
    4: "venue_course_4th", 5: "venue_course_5th", 6: "venue_course_6th",
}
_VENUE_KIMARITE_COLS = {
    "逃げ": "venue_kimarite_nige", "まくり": "venue_kimarite_makuri",
    "差し": "venue_kimarite_sashi", "まくり差し": "venue_kimarite_makuri_sashi",
    "抜き": "venue_kimarite_nuki", "恵まれ": "venue_kimarite_megumare",
}

# history_full.csv から入力として持ち出す列（結果の列は含めない）。
RACECARD_COLS = [
    "racer_id", "racer_name", "racer_class", "avg_st", "racer_win_rate",
    "local_win_rate", "motor_2ren", "boat_2ren", "f_count", "l_count",
    "weight", "exhibition_time", "tilt", "exhibition_st", "parts_exchanged",
    "temperature", "wind_speed", "water_temperature", "wave_height",
]
MEET_COLS = [
    "current_meet_races", "current_meet_avg_finish",
    "current_meet_avg_finish_adjusted", "current_meet_top2_rate",
    "current_meet_avg_st",
]
COURSE_COLS = ["course_top3_rate", "course_avg_st", "course_start_rank"]
VENUE_COLS = list(_VENUE_FINISH_COLS.values()) + list(_VENUE_KIMARITE_COLS.values())
INPUT_COLS = RACECARD_COLS + MEET_COLS + COURSE_COLS + VENUE_COLS
# Supabase 側で整数型の入力列（0.0 のような値を整数に直してから送る）。
INT_INPUT_COLS = {"f_count", "l_count", "parts_exchanged", "current_meet_races"}


def _ymd(d):
    return pd.Timestamp(d).strftime("%Y%m%d")


def _num(series):
    return pd.to_numeric(series, errors="coerce")


# ---------------------------------------------------------------
# データの読み込み
# ---------------------------------------------------------------
def rating_period(day):
    """その日に公式の選手ページが出しているコース別成績の集計期間（級別審査期間）。"""
    day = pd.Timestamp(day)
    if day.month >= 7:
        return pd.Timestamp(day.year - 1, 11, 1), pd.Timestamp(day.year, 4, 30)
    return pd.Timestamp(day.year - 1, 5, 1), pd.Timestamp(day.year - 1, 10, 31)


def _prepare_k(k):
    k = k.copy()
    for c in ("finish", "course", "st", "race_no", "day_no"):
        k[c] = _num(k[c])
    k["racer_id"] = k["racer_id"].astype(str).str.strip()
    k["jcd"] = k["jcd"].astype(str).str.zfill(2)
    k["d"] = pd.to_datetime(k["race_date"].astype(str), format="%Y%m%d")
    return k


def load_k_results(start, end, download_missing=True):
    """
    start〜end の各日の計算に必要な期間の競走成績（K）を読む。

    データ用リポジトリにまだ無い日（History Backfill は翌晩に取る）は、
    公式のダウンロードファイルを直接取得して補う（保存はしない）。
    取得できなかった日（未公開など）の集合も返す。
    """
    first_needed = min(rating_period(start)[0], pd.Timestamp(start) - pd.DateOffset(months=4))
    k = store.read_kind("k_results", start=_ymd(first_needed))
    have = set(k["race_date"].astype(str)) if len(k) else set()

    missing = set()
    extra = []
    # 今節成績に必要な直近（節は最長でも7日程度）の日だけを補う。
    day = pd.Timestamp(start) - pd.Timedelta(days=10)
    while day <= pd.Timestamp(end):
        d = _ymd(day)
        if d not in have:
            frame = None
            if download_missing:
                frame = _download_k(d)
            if frame is not None and len(frame):
                extra.append(frame.astype(str))
            else:
                missing.add(d)
        day += pd.Timedelta(days=1)
    if extra:
        k = pd.concat([k] + extra, ignore_index=True)
    return _prepare_k(k), missing


def _download_k(date_yyyymmdd):
    try:
        from official_download import download_text, parse_k
        text = download_text("K", date_yyyymmdd)
        if text is None:
            print(f"[HINDCAST] K {date_yyyymmdd}: 未公開", flush=True)
            return None
        boats, _ = parse_k(text, date_yyyymmdd)
        print(f"[HINDCAST] K {date_yyyymmdd}: 公式から取得 {len(boats)}行", flush=True)
        return boats
    except Exception as e:  # noqa: BLE001
        print(f"[HINDCAST] K {date_yyyymmdd}: 取得失敗 {type(e).__name__}: {e}", flush=True)
        return None


# ---------------------------------------------------------------
# その日の時点で分かっていた補助情報
# ---------------------------------------------------------------
def course_stats(k, day):
    """選手×進入コースの3連対率・平均ST・ST順位（公式の選手ページと同じ期間）。"""
    lo, hi = rating_period(day)
    w = k[(k["d"] >= lo) & (k["d"] <= hi) & k["course"].between(1, 6)].copy()
    w["top3"] = w["finish"].between(1, 3)
    w["st_rank"] = w[w["st"].notna()].groupby("race_key")["st"].rank(method="min")
    g = w.groupby(["racer_id", "course"])
    return pd.DataFrame({
        "course_top3_rate": g["top3"].mean() * 100.0,
        "course_avg_st": g["st"].mean(),
        "course_start_rank": g["st_rank"].mean(),
    })


def venue_profile(k, day):
    """場×進入コースの1〜6着率と、1着の決まり手の割合（前月までの3か月）。"""
    month_start = pd.Timestamp(day).to_period("M").start_time
    lo, hi = month_start - pd.DateOffset(months=3), month_start - pd.Timedelta(days=1)
    w = k[(k["d"] >= lo) & (k["d"] <= hi) & k["course"].between(1, 6) & k["finish"].notna()]
    finish = pd.crosstab([w["jcd"], w["course"]], w["finish"], normalize="index") * 100.0
    finish = finish.rename(columns=lambda c: _VENUE_FINISH_COLS.get(int(c), str(c)))
    win = w[w["finish"] == 1]
    kim = pd.crosstab([win["jcd"], win["course"]], win["kimarite"], normalize="index") * 100.0
    kim = kim.rename(columns=_VENUE_KIMARITE_COLS)
    out = finish.join(kim, how="outer")
    for c in VENUE_COLS:
        if c not in out.columns:
            out[c] = 0.0
    return out[VENUE_COLS].fillna(0.0)


def meet_days(k_days, jcd, day):
    """その日を含む節の開催日（日目が1ずつ減る方向へさかのぼる）。"""
    v = k_days.get(jcd)
    day = pd.Timestamp(day)
    if v is None or day not in v.index:
        return [day]
    out = [day]
    cur_d, cur_n = day, v.loc[day, "day_no"]
    for d in reversed(v.index[v.index < day]):
        if (cur_d - d).days > 3:
            break
        n = v.loc[d, "day_no"]
        if pd.notna(n) and pd.notna(cur_n):
            if n >= cur_n:
                break
        elif v.loc[d, "meet_title"] != v.loc[cur_d, "meet_title"]:
            break
        out.append(d)
        cur_d, cur_n = d, n
    return out


def current_meet(k_meet, racer_id, race_no, day, baseline):
    """本番の今節成績（current_meet_fetcher）と同じ計算を、そのレースより前の走りで行う。"""
    g = k_meet[
        (k_meet["racer_id"] == racer_id)
        & ((k_meet["d"] < day) | (k_meet["race_no"] < race_no))
    ]
    g = g[g["course"].between(1, 6) & g["st"].notna()]
    rec = {
        "current_meet_races": int(len(g)),
        "current_meet_avg_finish": np.nan,
        "current_meet_avg_finish_adjusted": np.nan,
        "current_meet_top2_rate": np.nan,
        "current_meet_avg_st": np.nan,
    }
    if len(g):
        rec["current_meet_avg_st"] = float(g["st"].mean())
    fin = g[g["finish"].between(1, 6)]
    if len(fin):
        rec["current_meet_avg_finish"] = float(fin["finish"].mean())
        rec["current_meet_top2_rate"] = float((fin["finish"] <= 2).mean() * 100.0)
        diffs = fin["finish"] - fin["course"].astype(int).map(baseline)
        if diffs.notna().any():
            rec["current_meet_avg_finish_adjusted"] = float(diffs.mean())
    return rec


def course_baseline(history_before):
    out = dict(_FALLBACK_BASELINE)
    lane = _num(history_before["lane"])
    finish = _num(history_before["finish"])
    ok = lane.between(1, 6) & finish.notna()
    if ok.any():
        out.update({int(a): float(b) for a, b in finish[ok].groupby(lane[ok].astype(int)).mean().items()})
    return out


# ---------------------------------------------------------------
# 1日分の予想
# ---------------------------------------------------------------
def build_race(rows, day, cstats, vprof, k_meet, baseline):
    """history_full の1レース分から、fetch_official_race と同じ形の DataFrame を作る。"""
    rows = rows.sort_values("lane")
    jcd = str(rows["jcd"].iloc[0]).zfill(2)
    rno = int(rows["race_no"].iloc[0])
    race = pd.DataFrame({"lane": _num(rows["lane"]).astype(int).to_numpy()})
    for c in RACECARD_COLS:
        values = rows[c].to_numpy() if c in rows.columns else [np.nan] * len(rows)
        race[c] = values
    race["racer_id"] = race["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    for c in RACECARD_COLS:
        if c not in ("racer_id", "racer_name", "racer_class"):
            race[c] = _num(race[c])

    for c in MEET_COLS + COURSE_COLS + VENUE_COLS:
        race[c] = np.nan
    for i, rec in race.iterrows():
        race.loc[i, MEET_COLS] = pd.Series(
            current_meet(k_meet, rec["racer_id"], rno, day, baseline)
        )[MEET_COLS].to_numpy()
        key = (rec["racer_id"], float(rec["lane"]))
        if key in cstats.index:
            race.loc[i, COURSE_COLS] = cstats.loc[key, COURSE_COLS].to_numpy()
        vkey = (jcd, float(rec["lane"]))
        if vkey in vprof.index:
            race.loc[i, VENUE_COLS] = vprof.loc[vkey, VENUE_COLS].to_numpy()
    race["current_meet_races"] = race["current_meet_races"].fillna(0).astype(int)

    race["venue"] = VENUES.get(jcd, rows["venue"].iloc[0])
    race["race_no"] = rno
    race["date"] = pd.Timestamp(day).strftime("%Y-%m-%d")
    return race


def predict_race(model, race, runtime):
    """auto_random_fix._process_candidate と同じ手順で予想と買い目候補を作る。"""
    final = predict(
        model,
        race,
        display_weight=runtime["display_weight"],
        weather_weight=runtime["weather_weight"],
        venue_course_weight=runtime["venue_course_weight"],
        original_display_scale=0.0,
    )
    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target_points = int(plan["point_count"])
    main_points = min(int(plan["main_n"]), target_points)
    favorite_lane, risk_score, _ = assess_favorite_risk(race, final)
    hedge_lane = favorite_lane if runtime["hedge_enabled"] and risk_score >= 2 else None
    tickets = rank_tickets(
        tri,
        odds=None,
        main_n=main_points,
        cover_n=target_points - main_points,
        longshot_n=0,
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0,
        hedge_lane=hedge_lane,
        use_odds=False,
        first=final,
        min_second_coverage=plan["min_second_coverage"],
        close_third_gap=None,
        close_third_coverage=4,
        second_favorite_n=second_favorite_n_for(final),
    )
    return final, tickets, confidence(final, race), plan, risk_score


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        v = float(v)
        return round(v, 6) if np.isfinite(v) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _as_int(v):
    v = _clean(v)
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def run_day(day, history_full, sample_history, k, runtime, code_sha, calibration=None):
    """1日分の (予想行, 入力行) を返す。

    calibration は的中確率の表示補正（hit_calibration）。その日より前のデータだけで
    作った補正のときに限り、補正後の値を残す（後で補正後の値の当てはまりを確かめるため）。
    """
    day = pd.Timestamp(day)
    d = _ymd(day)
    hf_day = history_full[history_full["race_date"] == d]
    if hf_day.empty:
        return [], [], None

    t0 = time.time()
    first_history = sample_history[sample_history["_d"] < day].drop(columns="_d")
    model = train(first_history, as_of=d)
    hf_before = history_full[history_full["race_date"] < d]
    train_info = {
        "first_rows": int(len(first_history)),
        "first_last_date": str(first_history["race_date"].max()) if len(first_history) else None,
        "position_rows": int(getattr(model, "_position_model_rows", 0)),
        "position_last_date": str(hf_before["race_date"].max()) if len(hf_before) else None,
    }

    cstats = course_stats(k, day)
    vprof = venue_profile(k, day)
    k_days = {
        jcd: g.groupby("d").agg(day_no=("day_no", "first"), meet_title=("meet_title", "first")).sort_index()
        for jcd, g in k[(k["d"] <= day) & (k["d"] >= day - pd.Timedelta(days=14))].groupby("jcd")
    }
    baseline = course_baseline(hf_before)

    # 補正の列はマイグレーション後にしか無いため、補正が読めたときだけ送る。
    # その日を含むデータで作った補正なら使わず空にする（作り直しで古い値が残らないように）。
    send_calibration = calibration is not None
    if calibration and str(calibration.get("data_end") or "").replace("-", "") >= d:
        calibration = None

    pred_rows, input_rows = [], []
    for race_key, rows in hf_day.groupby("race_key", sort=True):
        rows = rows.drop_duplicates("lane")
        if len(rows) < 3:
            continue
        jcd = str(rows["jcd"].iloc[0]).zfill(2)
        mdays = meet_days(k_days, jcd, day)
        k_meet = k[(k["jcd"] == jcd) & k["d"].isin(mdays)]
        race = build_race(rows, day, cstats, vprof, k_meet, baseline)
        try:
            final, tickets, conf, plan, risk = predict_race(model, race, runtime)
        except Exception as e:  # noqa: BLE001
            print(f"[HINDCAST] predict error {race_key}: {type(e).__name__}: {e}", flush=True)
            continue

        p_first = {int(r.lane): float(r.p_first) for r in final.itertuples()}
        favorite = max(p_first, key=p_first.get)
        combos = [str(c) for c in tickets["combo"]]
        trifecta_actual = str(rows["trifecta"].iloc[0] or "").strip() if "trifecta" in rows else ""
        if trifecta_actual.lower() == "nan" or not trifecta_actual[:1].isdigit():
            trifecta_actual = ""
        payout = _num(rows["trifecta_payout_per_100"]).iloc[0] if "trifecta_payout_per_100" in rows else np.nan
        settled = bool(trifecta_actual)
        hit_rank = combos.index(trifecta_actual) + 1 if settled and trifecta_actual in combos else None
        exhibition_count = int(_num(race["exhibition_time"]).between(6.0, 8.5).sum())

        raw_hit_prob = _clean(ticket_hit_probability(tickets))
        pred_rows.append({
            "race_key": race_key,
            "model_version": HINDCAST_MODEL_VERSION,
            "race_date": day.strftime("%Y-%m-%d"),
            "jcd": jcd,
            "venue": race["venue"].iloc[0],
            "race_no": int(race["race_no"].iloc[0]),
            "p_first": [round(p_first.get(lane, 0.0), 6) for lane in range(1, 7)],
            "favorite_lane": favorite,
            "favorite_prob": round(p_first[favorite], 6),
            "confidence": conf,
            "candidate_tickets": [
                {"combo": str(t.combo), "group": str(t.group), "prob": _clean(t.prob)}
                for t in tickets.itertuples()
            ],
            "candidate_count": len(combos),
            "candidate_hit_probability": raw_hit_prob,
            "favorite_risk_score": int(risk),
            "exhibition_count": exhibition_count,
            "trifecta_actual": trifecta_actual or None,
            "trifecta_payout": _as_int(payout) if settled else None,
            "winner_lane": int(trifecta_actual[0]) if settled else None,
            "favorite_hit": (int(trifecta_actual[0]) == favorite) if settled else None,
            "candidate_hit": (hit_rank is not None) if settled else None,
            "candidate_hit_rank": hit_rank,
        })
        if send_calibration:
            calibrated = apply_calibration(raw_hit_prob, calibration)
            pred_rows[-1]["candidate_hit_probability_calibrated"] = _clean(calibrated)
            pred_rows[-1]["hit_calibration_id"] = calibration.get("id") if calibrated is not None else None
        for rec in race.to_dict("records"):
            input_rows.append({
                "race_key": race_key,
                "lane": int(rec["lane"]),
                "race_date": day.strftime("%Y-%m-%d"),
                "input_version": INPUT_VERSION,
                **{c: _as_int(rec.get(c)) if c in INT_INPUT_COLS else _clean(rec.get(c))
                   for c in INPUT_COLS},
            })
    run_row = {
        "race_date": day.strftime("%Y-%m-%d"),
        "model_version": HINDCAST_MODEL_VERSION,
        "input_version": INPUT_VERSION,
        "race_count": len(pred_rows),
        "settings": {k_: _clean(v) for k_, v in runtime.items()},
        "train_info": train_info,
        "code_sha": code_sha,
    }
    print(
        f"[HINDCAST] {d}: {len(pred_rows)}レース {time.time() - t0:.0f}秒",
        flush=True,
    )
    return pred_rows, input_rows, run_row


# ---------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------
def _saved_dates(start, end):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    r = _request(
        "GET",
        f"{url}/rest/v1/{RUN_TABLE}",
        params=[
            ("select", "race_date"),
            ("model_version", f"eq.{HINDCAST_MODEL_VERSION}"),
            ("race_date", f"gte.{pd.Timestamp(start):%Y-%m-%d}"),
            ("race_date", f"lte.{pd.Timestamp(end):%Y-%m-%d}"),
        ],
        headers=_headers(),
        timeout=30,
    )
    return {str(row["race_date"]).replace("-", "") for row in r.json() or []}


def _upsert(table, rows, on_conflict, chunk=500):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    for i in range(0, len(rows), chunk):
        _request(
            "POST",
            f"{url}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=_headers("resolution=merge-duplicates,return=minimal"),
            json=rows[i:i + chunk],
            timeout=60,
        )


def _runtime():
    from auto_random_fix import _load_settings, _runtime_settings

    try:
        return _runtime_settings(_load_settings())
    except Exception as e:  # noqa: BLE001
        print(f"[HINDCAST] app_settings を読めないため既定値で予想: {e}", flush=True)
        return _runtime_settings({})


def _code_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            sha = ""
    return sha[:12]


def main():
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=yesterday)
    ap.add_argument("--dry-run", default="", help="Supabaseへ保存せず、このフォルダへJSONで書く")
    ap.add_argument("--redo", action="store_true", help="保存済みの日も作り直す")
    ap.add_argument("--max-minutes", type=float, default=300)
    args = ap.parse_args()
    deadline = time.time() + args.max_minutes * 60

    start, end = str(args.start), min(str(args.end), yesterday)
    days = [_ymd(d) for d in pd.date_range(pd.Timestamp(start), pd.Timestamp(end))]

    if not args.dry_run and not args.redo:
        saved = _saved_dates(start, end)
        days = [d for d in days if d not in saved]
    if not days:
        print("[HINDCAST] 対象日なし（すべて保存済み）", flush=True)
        return

    history_full = pd.read_csv(data_path("history_full.csv"), dtype=str, keep_default_na=False)
    history_full = history_full.replace({"": np.nan})
    history_full["race_date"] = history_full["race_date"].astype(str)
    days = [d for d in days if (history_full["race_date"] == d).any()]

    sample_history = pd.read_csv(data_path("sample_history.csv"))
    sample_history["_d"] = pd.to_datetime(sample_history["race_date"], errors="coerce")

    k, k_missing = load_k_results(days[0], days[-1], download_missing=not args.dry_run) if days else (None, set())
    runtime = _runtime() if not args.dry_run else _load_runtime_default()
    code_sha = _code_sha()
    calibration = latest_from_env() if not args.dry_run else None
    print(f"[HINDCAST] 補正 id={calibration['id'] if calibration else 'なし'}", flush=True)
    print(f"[HINDCAST] 版 {HINDCAST_MODEL_VERSION} / 対象 {len(days)}日 / code {code_sha}", flush=True)

    out_dir = Path(args.dry_run) if args.dry_run else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    for d in days:
        if time.time() >= deadline:
            print("[HINDCAST] 時間切れ（残りは次回）", flush=True)
            break
        # 今節成績にはその日と直前の数日の競走成績が要る。欠けていれば次回に回す。
        window = {_ymd(pd.Timestamp(d) - pd.Timedelta(days=i)) for i in range(8)}
        if window & k_missing:
            print(f"[HINDCAST] {d}: 競走成績（K）が未取得の日があるため次回に回す "
                  f"{sorted(window & k_missing)}", flush=True)
            continue
        preds, inputs, run_row = run_day(
            d, history_full, sample_history, k, runtime, code_sha, calibration,
        )
        if not preds:
            continue
        if out_dir:
            (out_dir / f"{d}_predictions.json").write_text(json.dumps(preds, ensure_ascii=False))
            (out_dir / f"{d}_inputs.json").write_text(json.dumps(inputs, ensure_ascii=False))
            (out_dir / f"{d}_run.json").write_text(json.dumps(run_row, ensure_ascii=False))
        else:
            # hindcast_runs を最後に書き、その日の保存が終わった印にする
            # （途中で止まった日は次回やり直す。upsertなので重複しない）。
            _upsert(INPUT_TABLE, inputs, "race_key,lane")
            _upsert(PREDICTION_TABLE, preds, "race_key,model_version")
            _upsert(RUN_TABLE, [run_row], "race_date,model_version")
        done += 1
    print(f"[HINDCAST] 完了 {done}日", flush=True)


def _load_runtime_default():
    from auto_random_fix import _runtime_settings

    return _runtime_settings({})


if __name__ == "__main__":
    main()
