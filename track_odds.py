from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from official_fetcher import VENUES, fetch_odds3t, fetch_race_result
from odds_rollup import rollup_and_prune, DEFAULT_RETENTION_DAYS


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

WATCHLIST_TABLE = "odds_watchlist"
SNAPSHOT_TABLE = "odds_snapshots"
PREDICTION_SNAPSHOT_TABLE = "prediction_snapshots"
RESULT_TABLE = "prediction_results"

# 締切時刻を取得できない場合の旧来の安全停止。
MAX_TRACK_MINUTES = 180

# Phase 3:
# 締切後はオッズ取得を止め、公式結果だけを定期確認する。
# 公式結果がすぐ反映されない場合に備え、締切後2時間までは結果確定を待つ。
RESULT_WAIT_MINUTES = 120

JST = ZoneInfo("Asia/Tokyo")


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


def _normalize_race_date(race_date):
    """YYYYMMDD / YYYY-MM-DD を YYYYMMDD に統一する。"""
    return str(race_date or "").strip().replace("-", "").replace("/", "")


def _iso_race_date(race_date):
    hd = _normalize_race_date(race_date)
    if len(hd) == 8 and hd.isdigit():
        return f"{hd[:4]}-{hd[4:6]}-{hd[6:8]}"
    return str(race_date or "")


def _race_key(race_date, jcd, rno):
    return f"{_normalize_race_date(race_date)}_{str(jcd).zfill(2)}_{int(rno)}"


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
        f"?race_date=eq.{requests.utils.quote(str(race_date), safe='')}"
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


def _fetch_deadline(race_date, jcd, rno):
    """
    BOAT RACE公式出走表から「締切予定時刻」の12個の時刻を直接読み取る。
    取得できなければ None を返す。
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

        window = page_text[pos:pos + 500]
        times = re.findall(
            r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)",
            window,
        )

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


# -------------------------------------------------
# Phase 3: 固定予想 → 公式結果 → prediction_results 自動保存
# -------------------------------------------------

def _load_prediction_snapshot(race_key):
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{PREDICTION_SNAPSHOT_TABLE}"
        f"?race_key=eq.{requests.utils.quote(str(race_key), safe='')}"
        "&select=*"
        "&limit=1"
    )
    r = requests.get(endpoint, headers=_headers(), timeout=20)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None


def _result_exists(race_key):
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{RESULT_TABLE}"
        f"?race_key=eq.{requests.utils.quote(str(race_key), safe='')}"
        "&select=race_key"
        "&limit=1"
    )
    r = requests.get(endpoint, headers=_headers(), timeout=20)
    r.raise_for_status()
    data = r.json()
    return bool(data)


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def _clean_json_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return v


def _snapshot_payload(snapshot):
    if not snapshot:
        return {}

    raw = snapshot.get("payload_json", "")
    try:
        payload = json.loads(raw) if raw else {}
    except Exception as e:
        raise ValueError(
            f"固定予想 payload_json を読めません: {type(e).__name__} {e}"
        ) from e

    if not isinstance(payload, dict):
        raise ValueError("固定予想 payload_json の形式が不正です。")

    return payload


def _snapshot_payout(snapshot, actual_combo, payout_per_100):
    payload = _snapshot_payload(snapshot)
    tickets = payload.get("tickets", []) or []

    hit_stake = 0
    for item in tickets:
        if not isinstance(item, dict):
            continue
        if str(item.get("combo", "")).strip() != str(actual_combo).strip():
            continue
        stake = _safe_int(item.get("stake", 0), 0)
        if stake > 0:
            hit_stake += stake

    payout100 = _safe_int(payout_per_100, 0)
    received = int(payout100 * hit_stake / 100) if hit_stake > 0 else 0
    return received, hit_stake


def _build_result_record(
    snapshot,
    race_date,
    jcd,
    rno,
    official_result,
):
    """
    result_tracker.save_race_result と同じ要領で、
    固定済み payload_json だけを使って prediction_results 1行を組み立てる。

    GitHub Actions では Streamlit Secrets を使わないため、
    result_tracker.py を経由せず、SUPABASE_URL / SUPABASE_KEY で直接保存する。
    """
    payload = _snapshot_payload(snapshot)

    final = payload.get("final", []) or []
    tickets = payload.get("tickets", []) or []
    research = payload.get("research", {}) or {}

    if not isinstance(final, list) or not final:
        raise ValueError("固定予想に final データがありません。")
    if not isinstance(tickets, list):
        raise ValueError("固定予想の tickets データ形式が不正です。")

    valid_final = [
        x for x in final
        if isinstance(x, dict)
        and 1 <= _safe_int(x.get("lane"), 0) <= 6
    ]
    if not valid_final:
        raise ValueError("固定予想から6艇予測を復元できません。")

    ranked_first = sorted(
        valid_final,
        key=lambda x: _safe_float(x.get("p_first"), -1.0),
        reverse=True,
    )
    p1_lane = _safe_int(ranked_first[0].get("lane"), 0)
    p1_prob = _safe_float(ranked_first[0].get("p_first"), 0.0)

    valid_tickets = [x for x in tickets if isinstance(x, dict)]
    ranked_tickets = sorted(
        valid_tickets,
        key=lambda x: (
            _safe_float(x.get("prob"), -1.0),
            _safe_float(x.get("expected_return"), -1.0),
        ),
        reverse=True,
    )

    if ranked_tickets:
        top = ranked_tickets[0]
        top_ticket = str(top.get("combo", ""))
        top_ticket_prob = _safe_float(top.get("prob"), 0.0)
        top_ticket_odds = _safe_float(top.get("odds"), None)
        top_ticket_stake = _safe_int(top.get("stake"), 0)
    else:
        top_ticket = ""
        top_ticket_prob = 0.0
        top_ticket_odds = None
        top_ticket_stake = 0

    purchased = [
        x for x in valid_tickets
        if _safe_int(x.get("stake"), 0) > 0
    ]
    total_stake = sum(_safe_int(x.get("stake"), 0) for x in purchased)

    first_actual = int(official_result["first"])
    second_actual = int(official_result["second"])
    third_actual = int(official_result["third"])
    actual_combo = f"{first_actual}-{second_actual}-{third_actual}"

    received, _hit_stake = _snapshot_payout(
        snapshot,
        actual_combo,
        official_result["trifecta_payout_per_100"],
    )

    hit_any_ticket = actual_combo in {
        str(x.get("combo", "")).strip() for x in purchased
    }
    hit_top_ticket = actual_combo == top_ticket
    predicted_first_hit = first_actual == p1_lane

    profit = int(received) - int(total_stake)
    roi = (received / total_stake) if total_stake > 0 else None

    ticket_keep = (
        "combo",
        "group",
        "prob",
        "odds",
        "expected_return",
        "stake",
    )
    ticket_payload = []
    for row in purchased:
        item = {}
        for c in ticket_keep:
            if c in row:
                item[c] = _clean_json_value(row.get(c))
        ticket_payload.append(item)

    lane_payload = []
    for row in sorted(valid_final, key=lambda x: _safe_int(x.get("lane"), 99)):
        item = {}
        for c in ("lane", "racer_name", "p_first", "reason"):
            if c in row:
                item[c] = _clean_json_value(row.get(c))
        lane_payload.append(item)

    research_payload = {}
    if isinstance(research, dict):
        for label, rows in research.items():
            if not isinstance(rows, list):
                continue
            cleaned_rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = {}
                for c in ("lane", "racer_name", "p_first", "reason"):
                    if c in row:
                        item[c] = _clean_json_value(row.get(c))
                cleaned_rows.append(item)
            if cleaned_rows:
                research_payload[str(label)] = cleaned_rows

    return {
        "saved_at": datetime.now(JST).isoformat(timespec="seconds"),
        "race_date": _iso_race_date(race_date),
        "venue": str(VENUES.get(str(jcd).zfill(2), str(jcd).zfill(2))),
        "race_no": int(rno),
        "race_key": _race_key(race_date, jcd, rno),
        "first_actual": first_actual,
        "second_actual": second_actual,
        "third_actual": third_actual,
        "trifecta_actual": actual_combo,
        "p1_lane": p1_lane,
        "p1_prob": p1_prob,
        "top_ticket": top_ticket,
        "top_ticket_prob": top_ticket_prob,
        "top_ticket_odds": top_ticket_odds,
        "top_ticket_stake": top_ticket_stake,
        "total_stake": int(total_stake),
        "payout": int(received),
        "profit": int(profit),
        "roi": roi,
        "hit_top_ticket": bool(hit_top_ticket),
        "hit_any_ticket": bool(hit_any_ticket),
        "predicted_first_hit": bool(predicted_first_hit),
        "tickets_json": json.dumps(
            ticket_payload,
            ensure_ascii=False,
        ),
        "lane_probs_json": json.dumps(
            {
                "final": lane_payload,
                "research": research_payload,
                "snapshot": {
                    "saved_at": snapshot.get("saved_at", ""),
                    "kind": snapshot.get("snapshot_kind", ""),
                },
            },
            ensure_ascii=False,
        ),
    }


def _insert_result(record):
    """
    race_key は事前に _result_exists() で確認する。
    念のため on_conflict=race_key のupsert形式にして、
    GitHub Actionsの重複実行にも耐える。
    """
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{RESULT_TABLE}"
        "?on_conflict=race_key"
    )
    r = requests.post(
        endpoint,
        headers=_headers(
            "resolution=merge-duplicates,return=minimal"
        ),
        json=record,
        timeout=30,
    )
    r.raise_for_status()


def _try_finalize_result(race_date, jcd, rno):
    """
    結果確定済みなら prediction_results へ保存して True。
    まだ結果が出ていなければ False。
    """
    ctx = _race_key(race_date, jcd, rno)

    # Phase 2で既に手動保存済みなら、重複保存せず追跡だけ終了。
    if _result_exists(ctx):
        _deactivate(race_date, jcd, rno)
        print(
            "[PHASE3] result already saved; watch stopped:",
            ctx,
            flush=True,
        )
        return True

    snapshot = _load_prediction_snapshot(ctx)
    if not snapshot:
        # 手動でオッズ追跡だけ開始したレース。
        # 固定予想がないので自動検証はしない。
        _deactivate(race_date, jcd, rno)
        print(
            "[PHASE3] no prediction snapshot; watch stopped without result save:",
            ctx,
            flush=True,
        )
        return True

    try:
        official_result = fetch_race_result(
            _normalize_race_date(race_date),
            str(jcd).zfill(2),
            int(rno),
        )
    except ValueError as e:
        # 公式結果がまだ未確定。次回Actionsで再確認する。
        print(
            "[PHASE3] result not ready:",
            ctx,
            str(e),
            flush=True,
        )
        return False

    record = _build_result_record(
        snapshot,
        race_date,
        jcd,
        rno,
        official_result,
    )

    _insert_result(record)

    # 保存成功後だけwatchlistを止める。
    _deactivate(race_date, jcd, rno)

    print(
        "[PHASE3] AUTO RESULT SAVED:",
        ctx,
        "actual=",
        record["trifecta_actual"],
        "payout=",
        record["payout"],
        "profit=",
        record["profit"],
        "hit=",
        record["hit_any_ticket"],
        flush=True,
    )
    return True


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

            # -------------------------------------------------
            # Phase 3:
            # 締切後はオッズ追跡を止めるが、watchlistは結果確定まで残す。
            # 公式結果が取れた回で prediction_results 保存 → active=false。
            # -------------------------------------------------
            if now_jst >= deadline:
                try:
                    done = _try_finalize_result(
                        race_date,
                        jcd,
                        rno,
                    )
                except Exception as e:
                    print(
                        "[PHASE3] finalize error:",
                        race_date,
                        jcd,
                        rno,
                        type(e).__name__,
                        str(e),
                        flush=True,
                    )
                    done = False

                if done:
                    continue

                # 結果が長時間出ない異常系だけ安全停止。
                if now_jst >= deadline + timedelta(minutes=RESULT_WAIT_MINUTES):
                    try:
                        _deactivate(race_date, jcd, rno)
                        print(
                            "[PHASE3] result wait timeout; watch stopped:",
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
                else:
                    print(
                        "[PHASE3] waiting for official result:",
                        race_date,
                        jcd,
                        rno,
                        flush=True,
                    )

                # 締切後は新しいオッズsnapshotを保存しない。
                continue

        # 締切時刻が取得できなかった場合の従来の最終安全策。
        if deadline is None and _is_expired(item.get("created_at")):
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

        # 締切前だけオッズを保存。
        try:
            # 他の呼び出し（締切取得・結果取得・race_key生成）は全て
            # 正規化済みの日付を渡している。ここだけ生の値を渡していたため、
            # Supabase側の列型によっては "2026-08-22" が返ってきて
            # 不正なURLになり、エラーも出さずに0行のまま
            # 追跡が機能しなくなる可能性があった。
            hd = _normalize_race_date(race_date)

            odds = fetch_odds3t(hd, jcd, rno)

            count = _save_odds(
                hd,
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
                flush=True,
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
                flush=True,
            )

    # -------------------------------------------------
    # 古いオッズ生データの集約とクリーンアップ
    # -------------------------------------------------
    # 追跡そのものが終わったあとに実行する。
    # ここで失敗しても追跡結果には影響しないので、例外は握りつぶす。
    try:
        rollup_and_prune(
            SUPABASE_URL,
            _headers,
            retention_days=int(
                os.environ.get("ODDS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
            ),
            snapshot_table=SNAPSHOT_TABLE,
            result_table=RESULT_TABLE,
            prediction_snapshot_table=PREDICTION_SNAPSHOT_TABLE,
        )
    except Exception as e:
        print(
            "[ROLLUP] 集約処理をスキップしました:",
            type(e).__name__,
            str(e),
            flush=True,
        )


if __name__ == "__main__":
    # Supabaseの一時的な障害で5分おきに失敗メールが飛ぶのを防ぐ。
    # 本当のバグはログに出るので、そちらで確認する。
    try:
        main()
    except Exception as e:
        print(
            "[TRACK_ODDS] 実行を中断しました:",
            type(e).__name__,
            str(e),
            flush=True,
        )
