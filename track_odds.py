from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import requests

from official_fetcher import fetch_odds3t


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

WATCHLIST_TABLE = "odds_watchlist"
SNAPSHOT_TABLE = "odds_snapshots"

# 追跡開始からこの時間を超えたレースは自動停止。
# GitHub Actionsを5分おきに動かしても、終了後ずっと取得し続けないための安全策。
MAX_TRACK_MINUTES = 180


def _headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _require_config():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_KEY がGitHub Actions Secretsに設定されていません。"
        )


def _load_watchlist():
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{WATCHLIST_TABLE}"
        "?select=race_date,jcd,rno,active,created_at"
        "&active=eq.true"
        "&order=created_at.asc"
    )
    r = requests.get(endpoint, headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def _deactivate(race_date, jcd, rno):
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{WATCHLIST_TABLE}"
        f"?race_date=eq.{race_date}"
        f"&jcd=eq.{str(jcd).zfill(2)}"
        f"&rno=eq.{int(rno)}"
    )
    r = requests.patch(
        endpoint,
        headers=_headers("return=minimal"),
        json={"active": False},
        timeout=20,
    )
    r.raise_for_status()


def _is_expired(created_at):
    if not created_at:
        return False

    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age > timedelta(minutes=MAX_TRACK_MINUTES)
    except Exception:
        return False


def _save_odds(race_date, jcd, rno, odds_df):
    if odds_df is None or len(odds_df) == 0:
        return 0

    fetched_at = datetime.now(timezone.utc).isoformat()

    payload = []
    for _, row in odds_df.iterrows():
        combo = str(row.get("combo", "")).strip()

        try:
            odd = float(row.get("odds"))
        except Exception:
            continue

        if not combo or odd < 1:
            continue

        payload.append(
            {
                "race_date": str(race_date),
                "jcd": str(jcd).zfill(2),
                "rno": int(rno),
                "combo": combo,
                "odds": odd,
                "fetched_at": fetched_at,
            }
        )

    if not payload:
        return 0

    endpoint = f"{SUPABASE_URL}/rest/v1/{SNAPSHOT_TABLE}"

    r = requests.post(
        endpoint,
        headers=_headers("return=minimal"),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()

    return len(payload)


def main():
    _require_config()

    watchlist = _load_watchlist()

    if not watchlist:
        print("[TRACK_ODDS] active watchlist is empty")
        return

    print(f"[TRACK_ODDS] active races={len(watchlist)}")

    for item in watchlist:
        race_date = str(item.get("race_date", "")).strip()
        jcd = str(item.get("jcd", "")).zfill(2)
        rno = int(item.get("rno", 0) or 0)

        if not race_date or not jcd or rno <= 0:
            continue

        if _is_expired(item.get("created_at")):
            try:
                _deactivate(race_date, jcd, rno)
                print(
                    "[TRACK_ODDS] auto stop:",
                    race_date,
                    jcd,
                    rno,
                )
            except Exception as e:
                print(
                    "[TRACK_ODDS] deactivate error:",
                    race_date,
                    jcd,
                    rno,
                    type(e).__name__,
                    str(e),
                )
            continue

        try:
            odds = fetch_odds3t(race_date, jcd, rno)

            count = _save_odds(
                race_date,
                jcd,
                rno,
                odds,
            )

            print(
                "[TRACK_ODDS] saved:",
                race_date,
                jcd,
                rno,
                "rows=",
                count,
            )

        except Exception as e:
            # 1レース失敗しても他の追跡対象は続行する。
            print(
                "[TRACK_ODDS] error:",
                race_date,
                jcd,
                rno,
                type(e).__name__,
                str(e),
            )


if __name__ == "__main__":
    main()
