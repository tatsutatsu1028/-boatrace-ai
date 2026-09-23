from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from today_schedule_fetcher import fetch_today_schedule, fetch_venue_deadlines

JST = ZoneInfo("Asia/Tokyo")
SCHEDULE_TABLE = "daily_schedule"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# 開催中止・開催終了の会場は締切一覧を取得しても意味が無いため対象外にする。
INACTIVE_STATUSES = {"開催終了", "中止"}


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
                f"[SCHEDULE_PREFETCH] transient API error; retry "
                f"{attempt + 2}/{attempts} in {delay}s: {last_error}"
            )
            time.sleep(delay)

    raise last_error


class ScheduleFetchError(RuntimeError):
    """開催一覧の取得に失敗し、Supabaseへ書き込むべきでない場合に送出する。"""


def _build_rows(date_str, *, with_deadlines):
    """
    その日の開催会場一覧（全24場）を取得し、Supabaseへupsertする行を組み立てる。

    with_deadlines=True（朝の全量取得）の場合のみ、開催中の会場について
    1R〜12Rの締切一覧も取得してdeadlines列に含める。
    with_deadlines=False（日中の状態差分更新）の場合は締切一覧を取得せず、
    ペイロードにdeadlinesキー自体を含めない。PostgREFTのupsert
    （resolution=merge-duplicates）はペイロードに含まれない列を更新しない
    ため、既存のdeadlines値は上書きされずそのまま残る。
    """
    now_iso = datetime.now(JST).isoformat(timespec="seconds")
    schedule = fetch_today_schedule(date_str)

    rows = []
    for record in schedule.to_dict("records"):
        code = str(record["jcd"]).zfill(2)
        holding = bool(record.get("holding"))
        status = str(record.get("status", "") or "").strip()

        row = {
            "race_date": date_str,
            "jcd": code,
            "holding": holding,
            "day_label": record.get("day_label") or "",
            "status": status,
            "next_race_no": record.get("next_race_no"),
            "next_race_time": record.get("next_race_time") or "",
            "fetched_at": now_iso,
            "updated_at": now_iso,
        }

        if with_deadlines:
            deadlines = {}
            if holding and status not in INACTIVE_STATUSES:
                deadlines = fetch_venue_deadlines(date_str, code)
            row["deadlines"] = {str(k): v for k, v in deadlines.items()}

        rows.append(row)

    # fetch_today_schedule は公式サイトへの接続失敗時も例外を出さず、
    # 全24場 holding=False の行を返す。これをそのままupsertすると、
    # 正常に保存済みの開催情報・締切一覧を「全場休み」で上書きしてしまう
    # （アプリ側も行があるためフォールバックせず、会場が一つも出なくなる）。
    # 実際に全場休みの日はほぼ無いため、開催0場は取得失敗として扱い書き込まない。
    if not any(r["holding"] for r in rows):
        raise ScheduleFetchError(
            f"開催中の会場を1場も取得できませんでした（date={date_str}）。"
            "公式サイトの取得失敗とみなし、Supabaseへは書き込みません。"
        )

    return rows


def upsert_rows(rows):
    if not rows:
        return

    url, _ = _cfg()
    endpoint = f"{url}/rest/v1/{SCHEDULE_TABLE}?on_conflict=race_date,jcd"
    _request(
        "POST",
        endpoint,
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=rows,
        timeout=30,
    )


def fetch_existing_rows(date_str):
    """Supabaseに保存済みの指定日の行を返す（無ければ空リスト）。"""
    url, _ = _cfg()
    endpoint = (
        f"{url}/rest/v1/{SCHEDULE_TABLE}"
        f"?race_date=eq.{date_str}&select=jcd,holding,status,deadlines"
    )
    response = _request("GET", endpoint, headers=_headers(), timeout=30)
    return response.json() or []


def missing_full_reason(existing_rows):
    """
    その日の全量取得（締切一覧込み）がまだ済んでいないとみなす理由を返す。
    済んでいれば None。

    - 行が1件も無い（朝の全量取得が未実行）
    - 開催中の会場が1場も無い（取得失敗時の「全場休み」行が残っている）
    - 開催中の会場なのにdeadlinesが空（締切一覧の取得だけ失敗している）
    """
    if not existing_rows:
        return "no rows"

    active = [
        r for r in existing_rows
        if r.get("holding")
        and str(r.get("status") or "").strip() not in INACTIVE_STATUSES
    ]
    if not any(r.get("holding") for r in existing_rows):
        return "no holding venues"

    missing = sorted(
        str(r.get("jcd", "")).zfill(2) for r in active if not r.get("deadlines")
    )
    if missing:
        return f"deadlines missing for {missing}"
    return None


def run(mode, date_str=None):
    """
    mode:
      full   : 常に開催一覧＋締切一覧を全量取得して保存する。
      ensure : 当日分の全量取得が済んでいれば何もしない。未取得・欠けが
               あれば full と同じ処理を行う（朝の複数回実行用）。
      status : 開催一覧の状態のみ更新する。ただし当日分の全量取得が
               済んでいなければ full に切り替える（朝の実行が遅延・欠落
               した場合の保険）。
    """
    date_str = date_str or datetime.now(JST).strftime("%Y%m%d")

    if mode in ("ensure", "status"):
        reason = missing_full_reason(fetch_existing_rows(date_str))
        if reason is None and mode == "ensure":
            print(
                f"[SCHEDULE_PREFETCH] mode=ensure date={date_str} "
                "already filled; nothing to do"
            )
            return
        if reason is not None:
            print(
                f"[SCHEDULE_PREFETCH] mode={mode} date={date_str} "
                f"full prefetch not done yet ({reason}); running full"
            )
            mode = "full"

    with_deadlines = mode == "full"

    rows = _build_rows(date_str, with_deadlines=with_deadlines)
    upsert_rows(rows)

    holding_count = sum(1 for r in rows if r["holding"])
    print(
        f"[SCHEDULE_PREFETCH] mode={mode} date={date_str} "
        f"rows={len(rows)} holding={holding_count}"
    )


def main():
    parser = argparse.ArgumentParser(description="開催スケジュールの事前取得・保存")
    parser.add_argument(
        "--mode",
        choices=["full", "ensure", "status"],
        default="full",
        help=(
            "full: 開催一覧＋締切一覧を全量取得（手動実行想定）。"
            "ensure: 当日分が未取得・欠けている場合だけfullを行う（朝の複数回実行想定）。"
            "status: 開催一覧の状態のみ再取得し、締切一覧は更新しない（日中の差分更新想定）。"
            "ただし当日分が未取得・欠けている場合はfullに切り替える。"
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help="対象日（YYYYMMDD）。省略時は実行時点のJST日付。",
    )
    args = parser.parse_args()
    run(args.mode, args.date)


if __name__ == "__main__":
    main()
