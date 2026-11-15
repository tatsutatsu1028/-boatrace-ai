"""perf.py（処理時間の計測・予想ボタンの打ち切り）。"""

from __future__ import annotations

import time

import pytest

import perf


def _spin(sec):
    end = time.perf_counter() + sec
    while time.perf_counter() < end:
        pass


def test_sections_are_exclusive_and_ordered():
    rec = perf.start("テスト")
    with perf.section("外"):
        _spin(0.05)
        with perf.section("内"):
            _spin(0.1)
    line = perf.finish(rec)
    outer, inner = rec.items["外"], rec.items["内"]
    assert inner[1] >= 0.08            # 内側の CPU
    assert outer[1] < inner[1]         # 外側からは内側を引いている
    assert line.index("内=") < line.index("外=")  # CPU の多い順
    assert perf._stack() == []


def test_order_keeps_given_stages():
    rec = perf.start("予想ボタン")
    with perf.section("保存"):
        pass
    line = perf.finish(rec, tag="[TIMING]", order=["学習読込", "保存"])
    assert line.startswith("[TIMING] 予想ボタン") and "保存=" in line and "学習読込" not in line


def test_laps_split_top_level_code():
    rec = perf.start("画面")
    perf.lap("A")
    _spin(0.03)
    perf.lap("B")
    with perf.section("B の中"):
        _spin(0.03)
    perf.finish(rec)
    assert set(rec.items) == {"A", "B", "B の中"}
    assert rec.items["B"][0] < rec.items["B の中"][0]


def test_deadline_stops_at_next_section_not_in_the_middle():
    page = perf.start("画面")
    rec = perf.start("予想ボタン", limit_seconds=0.05)
    done = []
    with perf.section("買い目"):
        _spin(0.08)        # 区間の途中では止めない
        done.append(1)
    with pytest.raises(perf.PredictionTimeout, match="予想ボタン"):
        with perf.section("保存"):
            done.append(2)
    perf.finish(rec)
    perf.finish(page)
    assert done == [1]


def test_listener_gets_stage_names():
    rec = perf.start("予想ボタン")
    seen = []
    rec.listener = seen.append
    with perf.section("学習読込"):
        with perf.section("新モデル予想"):
            pass
    perf.finish(rec)
    assert seen == ["学習読込", "新モデル予想"]


def test_section_without_recorder_is_noop():
    perf.abandon_all()
    with perf.section("x"):
        pass
    perf.lap("y")
    perf.check_deadline()
