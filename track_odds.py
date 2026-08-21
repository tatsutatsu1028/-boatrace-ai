from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

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
    """YYYYMMDD / YYYY-MM-DD を YYYYMMDD に統一する。"""
    return str(race_date or "").strip().replace("-", "").replace("/", "")


def _fetch_deadline(race_date, jcd, rno):
    """
    BOAT RACE公式出走表から「締切予定時刻」の12個の時刻を直接読み取る。
    取得できなければ None を返す（その回では停止させない）。
    """
    hd = _normalize_race_date(race_date)
    if len(hd) != 8:
        return None

    url = (
        "https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?hd={hd}&jcd={str(jcd).zfill(2)}&rno={int(rno)}"
    )

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                )
            },
            timeout=20,
        )
        r.raise_for_status()

        # HTMLをテキスト化して、「締切予定時刻」の直後だけを見る。
        soup = BeautifulSoup(r.text, "lxml")
        page_text = soup.get_text(" ", strip=True)

        pos = page_text.find("締切予定時刻")
        if pos < 0:
            print(
                "[TRACK_ODDS] deadline label not found:",
                hd, str(jcd).zfill(2), int(rno),
                flush=True,
            )
            return None

        # 締切予定時刻の後ろには通常12R分の HH:MM が連続している。
        window = page_text[pos:pos + 500]
        times = re.findall(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)", window)

        if len(times) < 12:
            print(
                "[TRACK_ODDS] deadline times insufficient:",
                hd, str(jcd).zfill(2), int(rno),
                "found=", len(times),
                "times=", times,
                flush=True,
            )
            return None

        hhmm = times[int(rno) - 1]
        hour, minute = map(int, hhmm.split(":"))

        race_day = datetime.strptime(hd, "%Y%m%d")
        return race_day.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=JST,
        )

    except Exception as e:
        print(
            "[TRACK_ODDS] deadline fetch error:",
            hd, str(jcd).zfill(2), int(rno),
            type(e).__name__, str(e),
            flush=True,
        )
        return None


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

        # 公式出走表の締切予定時刻を過ぎたら、その場で追跡停止。
        deadline = _fetch_deadline(race_date, jcd, rno)
        if deadline is not None:
            now_jst = datetime.now(JST)
            print(
                "[TRACK_ODDS] deadline check:",
                race_date,
                jcd,
                rno,
                "now=",
                now_jst.isoformat(timespec="minutes"),
                "deadline=",
                deadline.isoformat(timespec="minutes"),
                flush=True,
            )

            if now_jst >= deadline:
                try:
                    _deactivate(race_date, jcd, rno)
                    print(
                        "[TRACK_ODDS] deadline stop:",
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

        # 締切時刻が取得できなかった場合の最終安全策。
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