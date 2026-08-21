from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
from zoneinfo import ZoneInfo

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


JST = ZoneInfo("Asia/Tokyo")


def _normalize_race_date(race_date):
    """YYYYMMDD / YYYY-MM-DD のどちらでも YYYYMMDD にそろえる。"""
    s = str(race_date or "").strip()
    return s.replace("-", "").replace("/", "")


def _official_deadline(race_date, jcd, rno):
    """
    BOAT RACE公式の出走表から、そのレースの締切予定時刻を取得する。
    取得できなかった場合は None を返し、追跡は止めず次回再試行する。
    """
    hd = _normalize_race_date(race_date)
    if len(hd) != 8:
        return None

    url = (
        "https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?hd={hd}&jcd={str(jcd).zfill(2)}&rno={int(rno)}"
    )

    try:
        tables = pd.read_html(url)

        times = []
        for df in tables:
            for _, row in df.iterrows():
                cells = [str(v).strip() for v in row.tolist()]
                if not any("締切予定時刻" in c for c in cells):
                    continue

                row_times = []
                for c in cells:
                    import re
                    row_times.extend(re.findall(r"\b([01]?\d|2[0-3]):[0-5]\d\b", c))

                # 上の正規表現は時だけを返すため、行全体から直接取り直す
                joined = " ".join(cells)
                row_times = re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", joined)

                if row_times:
                    times.extend(row_times)

        if len(times) < int(rno):
            return None

        hhmm = times[int(rno) - 1]
        hour, minute = map(int, hhmm.split(":"))

        dt = datetime.strptime(hd, "%Y%m%d").replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=JST,
        )
        return dt

    except Exception as e:
        print(
            "[TRACK_ODDS] deadline fetch warning:",
            race_date,
            jcd,
            rno,
            type(e).__name__,
            str(e),
            flush=True,
        )
        return None


def _is_past_deadline(race_date, jcd, rno):
    """公式締切予定時刻を過ぎていれば True。取得不能時は False。"""
    deadline = _official_deadline(race_date, jcd, rno)
    if deadline is None:
        return False, None

    now = datetime.now(JST)
    return now >= deadline, deadline


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

        # まず公式の締切予定時刻を確認。
        # 締切を過ぎていれば、その回から取得せず active=false にする。
        try:
            past_deadline, deadline = _is_past_deadline(race_date, jcd, rno)
            if past_deadline:
                _deactivate(race_date, jcd, rno)
                print(
                    "[TRACK_ODDS] deadline stop:",
                    race_date,
                    jcd,
                    rno,
                    "deadline=",
                    deadline.isoformat() if deadline else "-",
                    flush=True,
                )
                continue
        except Exception as e:
            # 締切判定だけの一時エラーでは追跡を消さない。
            print(
                "[TRACK_ODDS] deadline check error:",
                race_date,
                jcd,
                rno,
                type(e).__name__,
                str(e),
                flush=True,
            )

        # 公式ページから締切時刻を取得できない場合に備えた最終安全策。
        if _is_expired(item.get("created_at")):
            try:
                _deactivate(race_date, jcd, rno)
                print(
                    "[TRACK_ODDS] fallback auto stop:",
                    race_date,
                    jcd,
                    rno,
                    flush=True,
                )
            except Exception as e:
                print(
                    "[TRACK_ODDS] deactivate error:",
                    race_date,
                    jcd,
                    rno,
                    type(e).__name__,
                    str(e),
                    flush=True,
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