from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from official_fetcher import fetch_race_result
from today_schedule_fetcher import fetch_venue_deadlines

JST = ZoneInfo("Asia/Tokyo")
COLLECTOR = "auto_random"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

RESULT_COLUMNS = [
    "saved_at", "race_date", "venue", "race_no", "race_key", "collector_name",
    "first_actual", "second_actual", "third_actual", "trifecta_actual",
    "p1_lane", "p1_prob", "top_ticket", "top_ticket_prob", "top_ticket_odds",
    "top_ticket_stake", "total_stake", "payout", "profit", "roi",
    "hit_top_ticket", "hit_any_ticket", "predicted_first_hit",
    "tickets_json", "lane_probs_json",
]


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


def _request(method, url, *, attempts=3, **kwargs):
    """一時的な接続失敗とData APIの5xxを指数バックオフで再試行する。"""
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"{response.status_code} Server Error for url: {response.url}",
                response=response,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc

        if attempt + 1 < attempts:
            delay = 2 ** attempt
            print(
                f"[AUTO_RESULT] transient API error; retry "
                f"{attempt + 2}/{attempts} in {delay}s: {last_error}"
            )
            time.sleep(delay)

    raise last_error


def _result_exists(race_key):
    url, _ = _cfg()
    r = _request(
        "GET",
        f"{url}/rest/v1/prediction_results",
        params={"select": "race_key", "race_key": f"eq.{race_key}", "limit": "1"},
        headers=_headers(),
        timeout=15,
    )
    return bool(r.json() or [])


def _pending_snapshots():
    """直近2日分の自動固定だけを見る。既に結果保存済みのものは後で除外。"""
    url, _ = _cfg()
    today = datetime.now(JST).date()
    start = (today - timedelta(days=1)).isoformat()
    r = _request(
        "GET",
        f"{url}/rest/v1/prediction_snapshots",
        params={
            "select": "race_key,race_date,venue,race_no,saved_at,snapshot_kind",
            "collector_name": f"eq.{COLLECTOR}",
            "race_date": f"gte.{start}",
            "order": "race_date.asc,race_no.asc",
        },
        headers=_headers(),
        timeout=20,
    )
    return r.json() or []


def _snapshot_payload(race_key):
    """処理対象になった1レース分だけ、容量の大きい予想本体を取得する。"""
    url, _ = _cfg()
    r = _request(
        "GET",
        f"{url}/rest/v1/prediction_snapshots",
        params={
            "select": "payload_json",
            "collector_name": f"eq.{COLLECTOR}",
            "race_key": f"eq.{race_key}",
            "limit": "1",
        },
        headers=_headers(),
        timeout=20,
    )
    rows = r.json() or []
    if not rows:
        raise ValueError(f"固定予想が見つかりません: {race_key}")
    return rows[0].get("payload_json")


def _parse_race_key(race_key):
    parts = str(race_key).split("_")
    if len(parts) != 3:
        raise ValueError(f"race_key形式不正: {race_key}")
    date_key, jcd, rno = parts
    if len(date_key) != 8:
        raise ValueError(f"race_key日付形式不正: {race_key}")
    return date_key, str(jcd).zfill(2), int(rno)


def _deadline_passed(date_key, jcd, rno, cache):
    """公式締切から5分以上経過したレースだけ結果ページを取りに行く。"""
    cache_key = (date_key, jcd)
    if cache_key not in cache:
        cache[cache_key] = fetch_venue_deadlines(date_key, jcd)
    hhmm = (cache[cache_key] or {}).get(int(rno))
    if not hhmm:
        return False
    hour, minute = [int(x) for x in str(hhmm).split(":")]
    d = datetime.strptime(date_key, "%Y%m%d")
    deadline = datetime(d.year, d.month, d.day, hour, minute, tzinfo=JST)
    return datetime.now(JST) >= deadline + timedelta(minutes=5)


def _safe_float(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _build_record(snapshot, official):
    try:
        payload = json.loads(snapshot.get("payload_json") or "{}")
    except Exception as e:
        raise ValueError("固定予想payload_jsonを読めません。") from e

    final_rows = payload.get("final") or []
    ticket_rows = payload.get("tickets") or []
    research = payload.get("research") or {}
    if not isinstance(final_rows, list) or not final_rows:
        raise ValueError("固定予想のfinalが空です。")
    if not isinstance(ticket_rows, list):
        raise ValueError("固定予想のtickets形式が不正です。")

    final = pd.DataFrame(final_rows)
    tickets = pd.DataFrame(ticket_rows)
    if not {"lane", "p_first"}.issubset(final.columns):
        raise ValueError("固定予想の1着確率を復元できません。")
    if len(tickets) and not {"combo", "stake"}.issubset(tickets.columns):
        raise ValueError("固定買い目を復元できません。")

    actual_combo = str(official["trifecta"])
    ranked_first = final.assign(
        p_first_num=pd.to_numeric(final["p_first"], errors="coerce")
    ).sort_values("p_first_num", ascending=False)
    p1_row = ranked_first.iloc[0]
    p1_lane = _safe_int(p1_row.get("lane"))
    p1_prob = _safe_float(p1_row.get("p_first"), 0.0)

    tickets = tickets.copy()
    if len(tickets):
        tickets["prob_num"] = pd.to_numeric(tickets.get("prob"), errors="coerce")
        if "expected_return" in tickets.columns:
            tickets["ev_num"] = pd.to_numeric(tickets["expected_return"], errors="coerce")
            ranked_tickets = tickets.sort_values(["prob_num", "ev_num"], ascending=False, na_position="last")
        else:
            ranked_tickets = tickets.sort_values("prob_num", ascending=False, na_position="last")

        top = ranked_tickets.iloc[0]
        top_ticket = str(top.get("combo", ""))
        top_ticket_prob = _safe_float(top.get("prob"), 0.0)
        top_ticket_odds = _safe_float(top.get("odds"), None)
        top_ticket_stake = _safe_int(top.get("stake"), 0)
        stake_num = pd.to_numeric(tickets["stake"], errors="coerce").fillna(0)
    else:
        top_ticket = ""
        top_ticket_prob = 0.0
        top_ticket_odds = None
        top_ticket_stake = 0
        tickets = pd.DataFrame(columns=["combo", "stake"])
        stake_num = pd.Series(dtype=float)
    total_stake = int(stake_num.sum())
    purchased = tickets[stake_num > 0].copy()
    purchased_combos = set(purchased["combo"].astype(str))
    hit_any = actual_combo in purchased_combos
    hit_top = actual_combo == top_ticket

    hit_stake = 0
    if hit_any:
        hit_rows = purchased[purchased["combo"].astype(str) == actual_combo]
        hit_stake = int(pd.to_numeric(hit_rows["stake"], errors="coerce").fillna(0).sum())
    payout = int(round(hit_stake * int(official["trifecta_payout_per_100"]) / 100))
    profit = payout - total_stake
    roi = payout / total_stake if total_stake > 0 else None

    keep_ticket_cols = ["combo", "group", "prob", "odds", "expected_return", "stake"]
    ticket_payload = []
    for _, row in purchased.iterrows():
        item = {}
        for c in keep_ticket_cols:
            if c in row.index:
                v = row[c]
                if pd.isna(v):
                    v = None
                elif hasattr(v, "item"):
                    v = v.item()
                item[c] = v
        ticket_payload.append(item)

    lane_payload = []
    for row in final_rows:
        if isinstance(row, dict):
            lane_payload.append(row)

    return {
        "saved_at": datetime.now(JST).isoformat(timespec="seconds"),
        "race_date": str(snapshot["race_date"]),
        "venue": str(snapshot["venue"]),
        "race_no": int(snapshot["race_no"]),
        "race_key": str(snapshot["race_key"]),
        "collector_name": COLLECTOR,
        "first_actual": int(official["first"]),
        "second_actual": int(official["second"]),
        "third_actual": int(official["third"]),
        "trifecta_actual": actual_combo,
        "p1_lane": p1_lane,
        "p1_prob": p1_prob,
        "top_ticket": top_ticket,
        "top_ticket_prob": top_ticket_prob,
        "top_ticket_odds": top_ticket_odds,
        "top_ticket_stake": top_ticket_stake,
        "total_stake": total_stake,
        "payout": payout,
        "profit": profit,
        "roi": roi,
        "hit_top_ticket": bool(hit_top),
        "hit_any_ticket": bool(hit_any),
        "predicted_first_hit": int(official["first"]) == p1_lane,
        "tickets_json": json.dumps(ticket_payload, ensure_ascii=False),
        "lane_probs_json": json.dumps({
            "final": lane_payload,
            "research": research if isinstance(research, dict) else {},
            "snapshot": {
                "saved_at": snapshot.get("saved_at", ""),
                "kind": snapshot.get("snapshot_kind", "auto_random"),
            },
        }, ensure_ascii=False),
    }


def _upsert_result(record):
    url, _ = _cfg()
    body = {k: record.get(k) for k in RESULT_COLUMNS}
    _request(
        "POST",
        f"{url}/rest/v1/prediction_results?on_conflict=race_key",
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=body,
        timeout=20,
    )


def main():
    snapshots = _pending_snapshots()
    if not snapshots:
        print("[AUTO_RESULT] no auto snapshots")
        return

    deadline_cache = {}
    saved = 0
    for snap in snapshots:
        race_key = str(snap.get("race_key", ""))
        if not race_key or _result_exists(race_key):
            continue

        try:
            date_key, jcd, rno = _parse_race_key(race_key)
            if not _deadline_passed(date_key, jcd, rno, deadline_cache):
                print("[AUTO_RESULT] not finished yet:", race_key)
                continue

            official = fetch_race_result(date_key, jcd, rno)
            snapshot = dict(snap)
            snapshot["payload_json"] = _snapshot_payload(race_key)
            record = _build_record(snapshot, official)
            _upsert_result(record)
            saved += 1
            print(
                "[AUTO_RESULT] saved",
                race_key,
                official["trifecta"],
                f"payout={record['payout']}",
                f"profit={record['profit']:+d}",
            )
        except Exception as e:
            # 結果未確定・中止・通信失敗などは次回実行で再試行する。
            print("[AUTO_RESULT] skip", race_key, type(e).__name__, e)

    print(f"[AUTO_RESULT] done saved={saved}")


if __name__ == "__main__":
    main()
