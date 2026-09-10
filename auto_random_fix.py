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
from prediction import train, predict, trifecta, rank_tickets
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


def _today_auto_count(date_text):
    url, _ = _cfg()
    r = requests.get(
        f"{url}/rest/v1/prediction_snapshots",
        params={
            "select": "race_key",
            "race_date": f"eq.{date_text}",
            "collector_name": f"eq.{COLLECTOR}",
        },
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return len(r.json() or [])


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


def _snapshot_payload(final, tickets):
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
    return {"final": final_rows, "tickets": ticket_rows, "research": {}}


def _save_snapshot(race_key, date_text, venue, rno, final, tickets):
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
        "payload_json": json.dumps(_snapshot_payload(final, tickets), ensure_ascii=False),
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


def _deadline_is_safe(today, hhmm, margin_minutes=20):
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
    締切20分以上前かつ未固定の候補をランダムに確認し、
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
            if not _deadline_is_safe(today, hhmm, margin_minutes=20):
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
    enabled = bool(settings.get("random_auto_enabled", False))
    target = int(settings.get("random_auto_daily_count", 3) or 3)
    target = max(1, min(target, 10))

    if not enabled:
        print("[AUTO_RANDOM] OFF")
        return

    now = datetime.now(JST)
    today = now.date()
    date_text = today.isoformat()
    current = _today_auto_count(date_text)
    if current >= target:
        print(f"[AUTO_RANDOM] target reached: {current}/{target}")
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

    history = pd.read_csv(Path(__file__).with_name("sample_history.csv"))
    model = train(history)
    final = predict(model, race)
    tri = trifecta(final)

    tickets = rank_tickets(
        tri,
        odds=odds,
        main_n=int(settings.get("main_n", 4) or 4),
        cover_n=int(settings.get("cover_n", 4) or 4),
        longshot_n=int(settings.get("hole_n", 0) or 0),
    )
    tickets = allocate_stakes_smart(
        tickets,
        budget=int(settings.get("total_budget", 2000) or 2000),
        unit=100,
        min_bet=int(settings.get("min_bet", 100) or 100),
        value_bias=float(settings.get("value_bias", 0) or 0),
        use_odds=False,
    )

    if _save_snapshot(race_key, date_text, venue, rno, final, tickets):
        print(f"[AUTO_RANDOM] saved {race_key}; daily {current + 1}/{target}")


if __name__ == "__main__":
    main()
