from __future__ import annotations

import json
import os
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from official_fetcher import VENUES, fetch_official_race, fetch_odds3t
from prediction import (
    train,
    predict,
    trifecta,
    rank_tickets,
    confidence,
    assess_favorite_risk,
    research_prediction_variants,
)
from stake_allocator import allocate_stakes_smart
from today_schedule_fetcher import fetch_today_schedule, fetch_venue_deadlines

JST = ZoneInfo("Asia/Tokyo")
COLLECTOR = "auto_random"
SNAPSHOT_KIND = "auto_random"


def _cfg():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です。")
    return url, key


def _headers(prefer=None):
    _, key = _cfg()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _load_settings():
    url, _ = _cfg()
    r = requests.get(
        f"{url}/rest/v1/app_settings?id=eq.1&select=*",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json() or []
    if not rows:
        raise RuntimeError("app_settings id=1 がありません。")
    return rows[0]


def _runtime_settings(settings):
    style = str(settings.get("prediction_style", "バランス"))
    default_display = 0.42 if style == "展示重視" else 0.32
    return {
        "main_n": int(settings.get("main_n", 3) or 3),
        "cover_n": int(settings.get("cover_n", 3) or 3),
        "hole_n": int(settings.get("hole_n", 0) or 0),
        "total_budget": int(settings.get("total_budget", 2000) or 2000),
        "min_bet": int(settings.get("min_bet", 100) or 100),
        "longshot_min_prob_pct": float(settings.get("longshot_min_prob_pct", 0.30) or 0.30),
        "value_bias": float(settings.get("value_bias", 0.0) or 0.0),
        "prediction_style": style,
        "display_weight": float(settings.get("display_weight", default_display)),
        "weather_weight": float(settings.get("weather_weight", 0.10)),
        "venue_course_weight": float(settings.get("venue_course_weight", 0.12)),
        "hedge_enabled": bool(settings.get("hedge_enabled", True)),
    }


def _run_auto_count(date_text, started_at):
    url, _ = _cfg()
    params = {
        "select": "race_key",
        "race_date": f"eq.{date_text}",
        "collector_name": f"eq.{COLLECTOR}",
    }
    if started_at:
        params["saved_at"] = f"gte.{started_at}"
    r = requests.get(
        f"{url}/rest/v1/prediction_snapshots",
        params=params,
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return len(r.json() or [])


def _set_enabled(enabled):
    url, _ = _cfg()
    r = requests.patch(
        f"{url}/rest/v1/app_settings?id=eq.1",
        headers=_headers("return=minimal"),
        json={"random_auto_enabled": bool(enabled)},
        timeout=15,
    )
    r.raise_for_status()


def _snapshot_exists(race_key):
    url, _ = _cfg()
    r = requests.get(
        f"{url}/rest/v1/prediction_snapshots",
        params={"select": "race_key", "race_key": f"eq.{race_key}", "limit": "1"},
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return bool(r.json() or [])


def _json_safe(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        v = float(v)
        return v if np.isfinite(v) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _fixed_research_rule_status(final, tickets):
    """固定時点で確定できるAだけ保存。B/C/Dは追跡オッズが必要なのでNone。"""
    try:
        p1_prob = float(pd.to_numeric(final["p_first"], errors="coerce").max())
    except Exception:
        p1_prob = 0.0

    try:
        t = tickets.copy()
        t["stake"] = pd.to_numeric(t.get("stake", 0), errors="coerce").fillna(0)
        purchased = t[t["stake"] > 0]
        mainline = purchased[purchased["group"].astype(str).str.strip().eq("本線")]
        a_ok = bool(p1_prob >= 0.80 and len(mainline))
    except Exception:
        a_ok = False

    return {"A": a_ok, "B": None, "C": None, "D": None}


def _snapshot_payload(final, tickets, research_variants=None, confidence_label=None):
    final_cols = [
        "lane", "racer_name", "p_first", "p_second", "p_third",
        "p_second_given_1", "p_second_given_2", "p_second_given_3",
        "p_second_given_4", "p_second_given_5", "p_second_given_6",
        "model_version", "reason", "kimarite_adjustment", "kimarite_effect_pct",
        "kimarite_starts", "kimarite_wins", "kimarite_dominant", "kimarite_available",
    ]
    ticket_cols = [
        "combo", "group", "prob", "odds", "expected_return", "stake", "stake_reason"
    ]

    final_rows = []
    for _, row in final.sort_values("lane").iterrows():
        final_rows.append({c: _json_safe(row[c]) for c in final_cols if c in row.index})

    ticket_rows = []
    for _, row in tickets.iterrows():
        ticket_rows.append({c: _json_safe(row[c]) for c in ticket_cols if c in row.index})

    research_payload = {}
    if research_variants:
        for label, variant_df in research_variants.items():
            if variant_df is None or len(variant_df) == 0:
                continue
            rows = []
            for _, row in variant_df.sort_values("lane").iterrows():
                item = {}
                for c in (
                    "lane", "racer_name", "p_first", "p_second", "p_third",
                    "model_version", "reason",
                ):
                    if c in row.index:
                        item[c] = _json_safe(row[c])
                rows.append(item)
            research_payload[str(label)] = rows

    payload = {
        "final": final_rows,
        "tickets": ticket_rows,
        "research": research_payload,
        "research_rules": _fixed_research_rule_status(final, tickets),
    }
    label = str(confidence_label or "").strip()
    if label in {"A", "B", "C"}:
        payload["confidence"] = label
    return payload


def _save_snapshot(
    race_key,
    date_text,
    venue,
    rno,
    final,
    tickets,
    research_variants=None,
    confidence_label=None,
):
    if _snapshot_exists(race_key):
        print("[AUTO_RANDOM] already fixed:", race_key)
        return False

    url, _ = _cfg()
    record = {
        "race_key": race_key,
        "collector_name": COLLECTOR,
        "saved_at": datetime.now(JST).isoformat(timespec="seconds"),
        "race_date": date_text,
        "venue": venue,
        "race_no": int(rno),
        "snapshot_kind": SNAPSHOT_KIND,
        "payload_json": json.dumps(
            _snapshot_payload(
                final,
                tickets,
                research_variants=research_variants,
                confidence_label=confidence_label,
            ),
            ensure_ascii=False,
        ),
    }
    r = requests.post(
        f"{url}/rest/v1/prediction_snapshots",
        headers=_headers("return=minimal"),
        json=record,
        timeout=20,
    )
    if r.status_code == 409:
        print("[AUTO_RANDOM] race fixed concurrently:", race_key)
        return False
    r.raise_for_status()
    return True


def _add_to_odds_watchlist(race_date, jcd, rno):
    """手動固定と同じく、固定後にオッズ追跡へ登録する。"""
    url, _ = _cfg()
    payload = {
        "race_date": str(race_date),
        "jcd": str(jcd).zfill(2),
        "rno": int(rno),
        "active": True,
    }
    r = requests.post(
        f"{url}/rest/v1/odds_watchlist?on_conflict=race_date,jcd,rno",
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()


def _save_odds_snapshot_now(race_date, jcd, rno, odds_df):
    """固定時点のオッズを1回保存し、B/C/Dの追跡起点を手動固定と揃える。"""
    if odds_df is None or len(odds_df) == 0:
        return 0

    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    payload = []
    for _, row in odds_df.iterrows():
        combo = str(row.get("combo", "")).strip()
        try:
            odd = float(row.get("odds"))
        except Exception:
            continue
        if not combo or odd < 1:
            continue
        payload.append({
            "race_date": str(race_date),
            "jcd": str(jcd).zfill(2),
            "rno": int(rno),
            "combo": combo,
            "odds": odd,
            "fetched_at": fetched_at,
        })

    if not payload:
        return 0

    url, _ = _cfg()
    r = requests.post(
        f"{url}/rest/v1/odds_snapshots",
        headers=_headers("return=minimal"),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return len(payload)


def _deadline_is_safe(today, hhmm, margin_minutes=15):
    try:
        hour, minute = [int(x) for x in str(hhmm).split(":")]
        deadline = datetime(today.year, today.month, today.day, hour, minute, tzinfo=JST)
        return (deadline - datetime.now(JST)).total_seconds() >= margin_minutes * 60
    except Exception:
        return False


def _exhibition_ready(race):
    """6艇すべての公式展示タイムが取得できたレースだけを固定対象にする。"""
    if race is None or len(race) != 6 or "exhibition_time" not in race.columns:
        return False

    lanes = pd.to_numeric(race.get("lane"), errors="coerce")
    times = pd.to_numeric(race["exhibition_time"], errors="coerce")
    valid = lanes.between(1, 6) & times.between(6.0, 8.5)
    return bool(valid.sum() == 6 and lanes[valid].nunique() == 6)


def _pick_candidate(today):
    """
    締切15分以上前かつ未固定の候補をランダムに確認し、
    公式展示タイムが6艇分そろったレースだけを返す。

    展示未公開の候補は固定せずスキップする。候補確認時に取得した
    race DataFrame をそのまま予想に使い、同じレースの再取得を避ける。
    """
    date_key = today.strftime("%Y%m%d")
    schedule = fetch_today_schedule(date_key)
    holding = schedule[schedule["holding"].astype(bool)].copy()
    codes = holding["jcd"].astype(str).str.zfill(2).tolist()
    random.shuffle(codes)

    candidates = []
    for jcd in codes:
        try:
            deadlines = fetch_venue_deadlines(date_key, jcd)
        except Exception as e:
            print("[AUTO_RANDOM] deadline error", jcd, type(e).__name__, e)
            continue

        for rno, hhmm in deadlines.items():
            if not _deadline_is_safe(today, hhmm, margin_minutes=15):
                continue
            race_key = f"{date_key}_{jcd}_{int(rno)}"
            if _snapshot_exists(race_key):
                continue
            candidates.append((jcd, int(rno), race_key))

    random.shuffle(candidates)

    for jcd, rno, race_key in candidates:
        try:
            race = fetch_official_race(date_key, jcd, rno)
        except Exception as e:
            print(
                "[AUTO_RANDOM] official data error",
                race_key,
                type(e).__name__,
                e,
            )
            continue

        if not _exhibition_ready(race):
            count = 0
            if race is not None and "exhibition_time" in race.columns:
                count = int(pd.to_numeric(race["exhibition_time"], errors="coerce").notna().sum())
            print(
                f"[AUTO_RANDOM] exhibition not ready: {race_key} ({count}/6); skip"
            )
            continue

        print(f"[AUTO_RANDOM] exhibition ready: {race_key} (6/6)")
        return jcd, rno, race_key, race

    return None


def main():
    settings = _load_settings()
    runtime = _runtime_settings(settings)
    enabled = bool(settings.get("random_auto_enabled", False))
    target = int(settings.get("random_auto_daily_count", 3) or 3)
    target = max(1, min(target, 10))
    started_at = settings.get("random_auto_started_at")

    if not enabled:
        print("[AUTO_RANDOM] OFF")
        return

    now = datetime.now(JST)
    today = now.date()
    date_text = today.isoformat()
    current = _run_auto_count(date_text, started_at)
    if current >= target:
        _set_enabled(False)
        print(f"[AUTO_RANDOM] target reached: {current}/{target}; switched OFF")
        return

    candidate = _pick_candidate(today)
    if candidate is None:
        print("[AUTO_RANDOM] no unfixed race with complete exhibition data found")
        return

    jcd, rno, race_key, race = candidate
    venue = VENUES.get(jcd, jcd)
    date_key = today.strftime("%Y%m%d")
    print("[AUTO_RANDOM] selected", race_key, venue, f"{rno}R", "exhibition=6/6")

    odds = fetch_odds3t(date_key, jcd, rno)
    if race is None or len(race) != 6 or not _exhibition_ready(race):
        raise RuntimeError("展示タイム6艇分を含む公式レースデータを取得できませんでした。")
    if odds is None or len(odds) < 100:
        raise RuntimeError("3連単オッズを十分に取得できませんでした。")

    # 本番学習データは手動固定と共通の sample_history.csv に固定。
    history = pd.read_csv(Path(__file__).with_name("sample_history.csv"))
    model = train(history)

    final = predict(
        model,
        race,
        display_weight=runtime["display_weight"],
        weather_weight=runtime["weather_weight"],
        venue_course_weight=runtime["venue_course_weight"],
        original_display_scale=0.0,
    )
    confidence_label = confidence(final, race)

    research_variants = research_prediction_variants(
        model,
        race,
        display_weight=runtime["display_weight"],
        weather_weight=runtime["weather_weight"],
        venue_course_weight=runtime["venue_course_weight"],
    )

    tri = trifecta(final)

    favorite_lane, risk_score, _risk_reasons = assess_favorite_risk(race, final)
    hedge_lane = (
        favorite_lane
        if runtime["hedge_enabled"] and risk_score >= 2
        else None
    )

    tickets = rank_tickets(
        tri,
        odds=odds,
        main_n=runtime["main_n"],
        cover_n=runtime["cover_n"],
        longshot_n=runtime["hole_n"],
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0,
        hedge_lane=hedge_lane,
        use_odds=False,
    )
    tickets = allocate_stakes_smart(
        tickets,
        budget=runtime["total_budget"],
        unit=100,
        min_bet=runtime["min_bet"],
        max_longshot_share=0.15,
        max_ticket_share=0.35,
        value_bias=runtime["value_bias"],
        use_odds=False,
    )

    if _save_snapshot(
        race_key,
        date_text,
        venue,
        rno,
        final,
        tickets,
        research_variants=research_variants,
        confidence_label=confidence_label,
    ):
        # 手動固定と同じく、固定直後からオッズ追跡を開始し、初回値も保存する。
        try:
            _add_to_odds_watchlist(date_key, jcd, rno)
            saved_odds = _save_odds_snapshot_now(date_key, jcd, rno, odds)
            print(f"[AUTO_RANDOM] odds tracking started: {race_key}; first={saved_odds}")
        except Exception as e:
            # 固定自体は成功しているため、追跡失敗で固定を取り消さない。
            print(
                "[AUTO_RANDOM] odds tracking start error",
                race_key,
                type(e).__name__,
                str(e),
            )

        new_count = current + 1
        print(
            f"[AUTO_RANDOM] saved {race_key}; confidence={confidence_label}; "
            f"run {new_count}/{target}"
        )
        if new_count >= target:
            _set_enabled(False)
            print("[AUTO_RANDOM] run completed; switched OFF")


if __name__ == "__main__":
    main()
