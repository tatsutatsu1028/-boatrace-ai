"""
締切の取り直し（schedule_prefetcher.refresh_pick_deadlines）。Supabase・公式サイトに繋がずに確かめる。

  python tests/test_schedule_deadlines.py   （pytest でも動く）
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule_prefetcher as sp  # noqa: E402


def _setup(picks, stored):
    calls = {"patch": [], "fetch": []}

    def get(path):
        if path.startswith("pre_exhibition_picks"):
            return [r for r in picks if r["t"] == "pre"]
        if path.startswith("manshu_chance_picks"):
            return [r for r in picks if r["t"] == "manshu"]
        if path.startswith(sp.SCHEDULE_TABLE):
            return [{"jcd": j, "deadlines": d} for j, d in stored.items()]
        raise AssertionError(path)

    sp._get, sp._patch = get, lambda path, body: calls["patch"].append((path, body))
    return calls


def test_changed_deadlines():
    old = {"1": "10:59", "2": "11:25"}
    assert sp.changed_deadlines(old, {"1": "11:14", "2": "11:25"}) == {"1": ("10:59", "11:14")}
    assert sp.changed_deadlines(old, {"1": "??"}) == {}          # 読めない値では変えない
    assert sp.changed_deadlines({}, {"3": "12:04"}) == {"3": (None, "12:04")}


def test_refresh_updates_schedule_and_picks():
    orig = sp._get, sp._patch
    try:
        picks = [{"t": "manshu", "race_key": "20261117_03_1", "deadline": "10:59"},
                 {"t": "manshu", "race_key": "20261117_03_5", "deadline": "12:43"},
                 {"t": "pre", "race_key": "20261117_08_7", "deadline": "13:20"},
                 {"t": "pre", "race_key": "20261117_18_4", "deadline": "09:53"}]   # 締切から30分以上前に終わった会場
        stored = {"03": {"1": "10:59", "5": "12:43", "12": "16:00"}, "08": {"7": "13:25"}, "18": {"4": "09:53"}}
        calls = _setup(picks, stored)
        official = {"03": {1: "11:14", 5: "12:56", 12: "16:13"}, "08": {7: "13:25"}}
        fetched, slept = [], []

        def fetch(d, j):
            fetched.append(j)
            return official[j]
        now = datetime(2026, 11, 17, 10, 50, tzinfo=sp.JST)
        n_v, n_r = sp.refresh_pick_deadlines("20261117", fetch=fetch, sleep=slept.append, now=now)
        assert fetched == ["03", "08"] and len(slept) == 1          # 18 は対象外・取得の間を空ける
        assert n_v == 1 and n_r == 3
        paths = [p for p, _ in calls["patch"]]
        assert paths[0] == "daily_schedule?race_date=eq.20261117&jcd=eq.03"
        assert calls["patch"][0][1]["deadlines"] == {"1": "11:14", "5": "12:56", "12": "16:13"}
        # 一覧の締切: 江戸川の2レースと、締切一覧は同じでも一覧だけ古い常滑のレース
        assert ("manshu_chance_picks?race_key=eq.20261117_03_5&result_at=is.null", {"deadline": "12:56"}) in calls["patch"]
        assert ("pre_exhibition_picks?race_key=eq.20261117_08_7&result_at=is.null", {"deadline": "13:25"}) in calls["patch"]
    finally:
        sp._get, sp._patch = orig


def test_refresh_keeps_going_when_a_venue_fails():
    orig = sp._get, sp._patch
    try:
        picks = [{"t": "manshu", "race_key": "20261117_01_4", "deadline": "16:43"},
                 {"t": "manshu", "race_key": "20261117_03_5", "deadline": "12:43"}]
        calls = _setup(picks, {"01": {"4": "16:43"}, "03": {"5": "12:43"}})

        def fetch(d, j):
            if j == "01":
                raise TimeoutError()
            return {5: "12:56"} if j == "03" else {}
        now = datetime(2026, 11, 17, 11, 0, tzinfo=sp.JST)
        assert sp.refresh_pick_deadlines("20261117", fetch=fetch, sleep=lambda s: None, now=now) == (1, 1)
        assert not any("jcd=eq.01" in p for p, _ in calls["patch"])
    finally:
        sp._get, sp._patch = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
