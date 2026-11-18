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


def _is_transient_auth_error(response):
    """
    Supabaseの一時的な401（PGRST303）かどうか。

    sb_secret_キーはゲートウェイで短命のJWTに変換されてからPostgRESTへ渡る。
    まれにそのJWTのクレーム検証が失敗して401（PGRST303）になるが、同じキーで
    直後に再試行すれば通る（9/28 8:05 JSTのprefetch_scheduleがこれで失敗した）。
    キー自体の誤りによる401（PGRST301など）は再試行しない。
    """
    if response.status_code != 401:
        return False
    return "PGRST303" in response.headers.get("proxy-status", "") or (
        "PGRST303" in response.text
    )


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
    """一時的な接続失敗とData APIの5xx・一時的な401を指数バックオフで再試行する。"""
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **kwargs)
            if (
                response.status_code not in RETRYABLE_STATUS
                and not _is_transient_auth_error(response)
            ):
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"{response.status_code} Error for url: {response.url}",
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
            "updated_at": now_iso,
        }

        if with_deadlines:
            # fetched_at は全量取得（締切一覧込み）の時刻として残す。日中の状態
            # 差分更新では送らず、既存値を上書きしない（updated_at だけ進める）。
            row["fetched_at"] = now_iso
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


# 締切の取り直し（日中の status の回）: 一覧（厳選10・万舟チャンス）にまだ結果の無いレースがある会場だけ、
# 公式の出走表から締切を取り直し、変わっていれば daily_schedule.deadlines と一覧の deadline を書き換える。
# 朝の全量取得の後に締切が変わる（江戸川の遅延など）と、展示後の自動予想・締切前の通知・結果の記録が
# 古い締切で動いてしまうため（2026-11-17 江戸川1R: 10:59 → 11:14）。
PICK_TABLES = ("pre_exhibition_picks", "manshu_chance_picks")
DEADLINE_FETCH_INTERVAL_SEC = 3.4   # 公式サイトへのアクセスの間隔（polite_http と同じ）


def _get(path):
    url, _ = _cfg()
    return _request("GET", f"{url}/rest/v1/{path}", headers=_headers(), timeout=30).json() or []


def _patch(path, body):
    url, _ = _cfg()
    _request("PATCH", f"{url}/rest/v1/{path}", headers=_headers("return=minimal"), json=body, timeout=30)


def _hhmm(v):
    try:
        h, m = (int(x) for x in str(v).split(":"))
        return h * 60 + m
    except (TypeError, ValueError):
        return None


def changed_deadlines(old, new):
    """{R: "HH:MM"} の新旧 → 変わったレース {R: (旧, 新)}。新しい値が読めないレースは変えない。"""
    out = {}
    for r, v in (new or {}).items():
        if _hhmm(v) is None:
            continue
        if str((old or {}).get(str(r)) or "") != str(v):
            out[str(r)] = ((old or {}).get(str(r)), str(v))
    return out


def refresh_pick_deadlines(date_str, fetch=None, sleep=time.sleep, now=None):
    """一覧のレースがある会場の締切を取り直す。書き換えた (会場数, 一覧の行数) を返す。"""
    fetch = fetch or fetch_venue_deadlines
    now = now or datetime.now(JST)
    iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    picks = []
    for table in PICK_TABLES:
        try:
            rows = _get(f"{table}?race_date=eq.{iso}&result_at=is.null&select=race_key,deadline")
        except Exception as e:  # noqa: BLE001  表が無いなど。開催状況の更新は止めない
            print(f"[SCHEDULE_PREFETCH] {table} を読めない {type(e).__name__}", flush=True)
            continue
        picks += [(table, r) for r in rows]
    # 締切を過ぎてから時間のたったレースしか無い会場は取り直さない（もう動かす処理が無い）
    venues = sorted({str(r["race_key"]).split("_")[1] for _, r in picks
                     if (_hhmm(r.get("deadline")) or 0) + 30 >= now.hour * 60 + now.minute})
    if not venues:
        return 0, 0
    stored = {str(r["jcd"]).zfill(2): r.get("deadlines") or {}
              for r in _get(f"{SCHEDULE_TABLE}?race_date=eq.{date_str}&select=jcd,deadlines")}
    n_venues = n_rows = 0
    for i, jcd in enumerate(venues):
        if i:
            sleep(DEADLINE_FETCH_INTERVAL_SEC)
        try:
            new = {str(k): v for k, v in (fetch(date_str, jcd) or {}).items()}
        except Exception as e:  # noqa: BLE001
            print(f"[SCHEDULE_PREFETCH] 締切の取り直しに失敗 jcd={jcd} {type(e).__name__}", flush=True)
            continue
        if not new:
            continue
        diff = changed_deadlines(stored.get(jcd), new)
        if diff:
            _patch(f"{SCHEDULE_TABLE}?race_date=eq.{date_str}&jcd=eq.{jcd}",
                   {"deadlines": {**stored.get(jcd, {}), **new}})
            n_venues += 1
            print(f"[SCHEDULE_PREFETCH] 締切の変更 jcd={jcd} "
                  + " ".join(f"{r}R {a}→{b}" for r, (a, b) in sorted(diff.items(), key=lambda kv: int(kv[0]))),
                  flush=True)
        for table, r in picks:
            _, pj, rno = str(r["race_key"]).split("_")
            want = new.get(str(int(rno)))
            if pj == jcd and want and _hhmm(want) is not None and str(r.get("deadline") or "") != want:
                _patch(f"{table}?race_key=eq.{r['race_key']}&result_at=is.null", {"deadline": want})
                n_rows += 1
    if n_venues or n_rows:
        print(f"[SCHEDULE_PREFETCH] 締切を書き換え 会場 {n_venues}・一覧 {n_rows}行", flush=True)
    return n_venues, n_rows


def run(mode, date_str=None):
    """
    mode:
      full   : 常に開催一覧＋締切一覧を全量取得して保存する。
      ensure : 当日分の全量取得が済んでいれば何もしない。未取得・欠けが
               あれば full と同じ処理を行う（朝の複数回実行用）。
      status : 開催一覧の状態のみ更新する。ただし当日分の全量取得が
               済んでいなければ full に切り替える（朝の実行が遅延・欠落
               した場合の保険）。続けて、一覧（厳選10・万舟チャンス）のレースが
               ある会場だけ締切を取り直す（refresh_pick_deadlines）。
    """
    date_str = date_str or datetime.now(JST).strftime("%Y%m%d")

    if mode == "status":
        # 日中の30分おきの回は、公式サイトに一時的に断られて（403など）取れなくても
        # 次の回で取り直せる。失敗のたびに通知が来ないよう、ログを残して正常終了する。
        # 朝の全量取得（prefetch_schedule.yml の full / ensure）は従来どおり失敗にする。
        try:
            _run(mode, date_str)
        except ScheduleFetchError as e:
            print(f"[SCHEDULE_PREFETCH] mode=status 取得できず、次の回に任せます: {e}", flush=True)
        return
    _run(mode, date_str)


def _run(mode, date_str):
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
    if mode == "status":
        # 日中の回: 一覧のレースがある会場の締切を取り直す（失敗しても開催状況の更新は済んでいる）
        try:
            refresh_pick_deadlines(date_str)
        except Exception as e:  # noqa: BLE001
            print(f"[SCHEDULE_PREFETCH] 締切の取り直しに失敗 {type(e).__name__}: {e}", flush=True)


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
