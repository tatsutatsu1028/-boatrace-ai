"""
公式サイトへ負荷をかけないための共通HTTP取得。

- リクエストの開始間隔を、全スレッド合計で MIN_INTERVAL 秒以上あける
  （既定 3.4秒 = 毎秒0.3リクエスト以下。1.5秒以上の条件も満たす）
- 応答待ちの間に次のリクエストを始められるよう、数本までの並列は許すが、
  開始間隔の制限は全体で共有するので、並列数を増やしても速度は上がらない
- 公式サイトのメンテナンス時間（毎日 4:00〜4:30 JST）は待機する
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.5; personal-analysis-tool; history-collector)"
}

# 毎秒0.3リクエスト以下 → 開始間隔 1/0.3 = 3.33秒。余裕をみて3.4秒。
MIN_INTERVAL = float(os.environ.get("POLITE_MIN_INTERVAL", "3.4"))
assert MIN_INTERVAL >= 1.5

_lock = threading.Lock()
_next_start = 0.0
_count = 0


def request_count():
    return _count


def now_jst():
    return datetime.now(JST)


def in_maintenance(t=None):
    t = t or now_jst()
    return t.hour == 4 and t.minute < 32   # 4:00〜4:30 + 余裕2分


def _wait_turn():
    global _next_start, _count
    while in_maintenance():
        time.sleep(30)
    with _lock:
        now = time.monotonic()
        start = max(now, _next_start)
        _next_start = start + MIN_INTERVAL
        _count += 1
    delay = start - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def get(url, timeout=30, binary=False):
    """1回だけ取得する（再試行は呼び出し側で判断）。"""
    _wait_turn()
    r = requests.get(url, headers=UA, timeout=timeout)
    if not binary:
        r.encoding = "utf-8"   # boatrace.jp はUTF-8固定（判定処理は遅いので省く）
    return r


def deadline_from_stop_at(stop_at, max_hours_ahead=10):
    """'03:50' のようなJST時刻 → time.time() 基準の締切。

    起動が遅れて停止時刻を過ぎてから始まった場合、翌日の同時刻まで
    走り続けないよう、max_hours_ahead より先になる場合は「もう過ぎた」扱い
    （= 今すぐ終了）にする。
    """
    import time as _time
    if not stop_at:
        return None
    hh, mm = map(int, stop_at.split(":"))
    now = now_jst()
    stop = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= now:
        stop += timedelta(days=1)
    ahead = (stop - now).total_seconds()
    if ahead > max_hours_ahead * 3600:
        return _time.time()
    return _time.time() + ahead
