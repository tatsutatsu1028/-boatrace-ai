"""
「🌅 展示前厳選10」と「💥 万舟チャンス」の一覧のレース（1日最大20レース）の結果を、その日のうちに書き込む。

公式サイトへのアクセスを増やさないため、
  1. 既存の結果確定（track_odds.py 5分おき・auto_result_collect.py 15分おき）が公式の結果ページを読んだとき、
     そのレースが一覧にあれば同じ結果を書き込む（on_official_result。追加のアクセスなし）
  2. どちらにも拾われないレース（予想を保存していないレース）だけ、締切の WAIT_MINUTES 分後以降に
     auto_result_collect.py の最後で1回ずつ結果ページを読む（sync_pending。1日最大20レース・まだ結果が
     出ていなければ次の回に読む）
書き込む列は翌朝のまとめての記録（pre_picks.py record_results / record_manshu_results）と同じ。
翌朝の記録は result_at が空のレースだけを対象にするので、ここで取りこぼしたレース（同着・中止など）の保険になる。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
TABLES = ("pre_exhibition_picks", "manshu_chance_picks")
# 締切からこの分数が過ぎても既存の結果確定に拾われていなければ、自分で結果ページを読む
WAIT_MINUTES = 20


def log(*a):
    print("[LIST_RESULTS]", *a, flush=True)


def _sb(method, path, **kwargs):
    from pre_picks import _sb as sb

    return sb(method, path, **kwargs)


def _fields(table, row, pays):
    import pre_picks

    if table == "pre_exhibition_picks":
        return pre_picks.result_fields(row, pays)
    return pre_picks.manshu_result_fields(row, pays)


def _pays(official):
    return [(str(official["trifecta"]), float(official["trifecta_payout_per_100"]))]


def apply_result(race_key, official, rows=None):
    """1レースの公式の結果（fetch_race_result の戻り値）を、一覧にあれば書き込む。書いた行の数を返す。"""
    done = 0
    for table in TABLES:
        if rows is not None:
            targets = [r for r in rows.get(table, []) if r["race_key"] == race_key]
        else:
            targets = _sb("GET", f"{table}?race_key=eq.{race_key}&result_at=is.null&select=*").json() or []
        for row in targets:
            _sb("PATCH", f"{table}?race_key=eq.{race_key}&result_at=is.null", prefer="return=minimal",
                json=_fields(table, row, _pays(official)))
            done += 1
    return done


def on_official_result(race_key, official):
    """既存の結果確定から呼ぶ。失敗しても結果確定は止めない。"""
    try:
        n = apply_result(race_key, official)
        if n:
            log(f"{race_key}: 一覧の結果を書き込み（{n}件）")
    except Exception as e:  # noqa: BLE001
        log(f"{race_key}: 一覧の結果を書けない {type(e).__name__}")


def pending(now=None):
    """結果がまだ無い一覧のレース（今日と昨日）{table: [行]}。"""
    now = now or datetime.now(JST)
    start = (now.date() - timedelta(days=1)).isoformat()
    out = {}
    for table in TABLES:
        try:
            out[table] = _sb("GET", f"{table}?race_date=gte.{start}&result_at=is.null&select=*").json() or []
        except Exception as e:  # noqa: BLE001  表がまだ無いなど
            log(f"{table} を読めない {type(e).__name__}")
            out[table] = []
    return out


def _deadline(row):
    try:
        y, m, d = (int(x) for x in str(row["race_date"]).split("-"))
        hh, mm = (int(x) for x in str(row.get("deadline") or "").split(":"))
        return datetime(y, m, d, hh, mm, tzinfo=JST)
    except Exception:  # noqa: BLE001
        return None


def due_races(rows, now=None, wait_minutes=WAIT_MINUTES):
    """締切から wait_minutes 分以上たった、結果の無いレースの race_key（重複なし・締切順）。"""
    now = now or datetime.now(JST)
    due = {}
    for table_rows in rows.values():
        for r in table_rows:
            dl = _deadline(r)
            if dl is not None and now >= dl + timedelta(minutes=wait_minutes):
                due.setdefault(r["race_key"], dl)
    return [rk for rk, _ in sorted(due.items(), key=lambda kv: kv[1])]


def sync_pending(fetch=None, now=None):
    """既存の結果確定に拾われなかった一覧のレースだけ、結果ページを1回ずつ読んで書き込む。"""
    if fetch is None:
        from official_fetcher import fetch_race_result as fetch
    rows = pending(now)
    keys = due_races(rows, now)
    done = waiting = 0
    for rk in keys:
        date, jcd, rno = rk.split("_")
        try:
            official = fetch(date, jcd, int(rno))
        except ValueError:
            waiting += 1  # まだ結果が出ていない（中止・不成立は翌朝の記録が書く）
            continue
        except Exception as e:  # noqa: BLE001
            log(f"{rk}: 結果を読めない {type(e).__name__}")
            continue
        try:
            apply_result(rk, official, rows)
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"{rk}: 書き込めない {type(e).__name__}")
    if keys:
        log(f"一覧の結果: 対象 {len(keys)}レース・書き込み {done}・結果待ち {waiting}")
