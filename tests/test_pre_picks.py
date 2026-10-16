"""
「🌅 展示前厳選10」（ml_pre.py・pre_picks.py・pre_picks_view.py）の、データ用リポジトリが無くても
確かめられる部分。

  python tests/test_pre_picks.py   （pytest でも動く）
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_pre  # noqa: E402
import pre_picks  # noqa: E402
import pre_picks_view  # noqa: E402


def test_no_exhibition_features():
    """展示・直前情報の列は展示前モデルの特徴量に入らない。想定コースは艇番になる。"""
    for c in ("exhibition_time", "exhibition_st", "tilt", "parts_exchanged", "propeller_new", "adjust_weight",
              "wind_speed", "wave_cm", "rk_exh_time", "st12_ex_diff", "front_entry"):
        assert c not in ml_pre.PRE_FEATURES, c
        assert c not in ml_pre.PRE_LANE_FREE, c
    assert "exhibition_course" in ml_pre._PRE_BLANK_BASE


def test_morning_meet_uses_only_previous_days():
    """その日の2走目にも、1走目と同じ「前日まで」の今節成績を使う（当日の結果を混ぜない）。"""
    e = pd.DataFrame({
        "race_key": ["d1_1", "d2_1", "d2_8"],
        "lane": [1, 1, 3],
        "racer_id": ["4001"] * 3,
        "d": pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-02"]),
        "race_no": [1, 1, 8],
    })
    # 当日の1走目の後は meet_n=2（当日の1R の結果を含む）になっている
    meet = pd.DataFrame({"race_key": ["d1_1", "d2_1", "d2_8"], "lane": [1, 1, 3],
                         "meet_n": [0.0, 1.0, 2.0], "meet_fin": [np.nan, 2.0, 1.5]})
    m = ml_pre.morning_meet(meet, e).set_index("race_key")
    assert m.loc["d2_8", "meet_n"] == 1.0
    assert m.loc["d2_8", "meet_fin"] == 2.0
    assert m.loc["d1_1", "meet_n"] == 0.0 and np.isnan(m.loc["d1_1", "meet_fin"])


def test_ticket_result_and_void():
    tickets = [{"combo": "1-2-3", "stake": 600}, {"combo": "1-3-2", "stake": 400}]
    assert pre_picks.ticket_result(tickets, [("1-2-3", 1230)]) == (True, 1000, 7380)
    assert pre_picks.ticket_result(tickets, [("2-1-3", 4560)]) == (False, 1000, 0)
    # 同着で3連単が2つ出たとき
    assert pre_picks.ticket_result(tickets, [("1-2-3", 1000), ("1-3-2", 2000)]) == (True, 1000, 14000)
    row = {"tickets": tickets, "post_tickets": [{"combo": "2-1-3", "stake": 2000}]}
    out = pre_picks.result_fields(row, [("2-1-3", 4560)])
    assert out["result_status"] == "settled" and out["trifecta_payout"] == 4560
    assert out["pre_hit"] is False and out["post_hit"] is True and out["post_return"] == 91200
    assert pre_picks.result_fields(row, None)["result_status"] == "void"
    assert pre_picks.result_fields(row, [("返還", None)])["result_status"] == "void"


def test_select_picks_order():
    c = pd.DataFrame({"race_key": list("abcdefghijkl"), "hit_probability": np.linspace(0.3, 0.8, 12),
                      "deadline": ["10:00"] * 12})
    p = pre_picks.select_picks(c)
    assert len(p) == 10 and p.iloc[0]["race_key"] == "l" and list(p["pick_rank"]) == list(range(1, 11))


def test_summary_uses_post_tickets_when_predicted():
    rows = [
        {"race_date": "2026-10-16", "trifecta_payout": 1000, "pre_hit": True, "pre_stake": 2000, "pre_return": 3000,
         "post_tickets": [{"combo": "1-2-3"}], "post_hit": False, "post_stake": 2000, "post_return": 0,
         "post_flag": True},
        {"race_date": "2026-10-16", "trifecta_payout": 3000, "pre_hit": False, "pre_stake": 2000, "pre_return": 0,
         "post_tickets": None, "post_hit": None, "post_stake": None, "post_return": None, "post_flag": None},
    ]
    s = pre_picks_view.summarize(rows)
    assert s["レース数"] == 2 and s["的中率（使った買い目）"] == 0.0 and s["回収率（使った買い目）"] == 0.0
    assert s["的中率（展示前の買い目）"] == 0.5 and s["回収率（展示前の買い目）"] == 0.75
    assert s["展示後に印が付いた割合"] == 0.5


def test_ticket_text_hides_zero_stake():
    t = [{"combo": "1-2-3", "stake": 600}, {"combo": "2-1-4", "stake": 0}]
    assert pre_picks_view.ticket_text(t) == "1-2-3 600円"


def test_fetch_racelists_keeps_order_and_failures():
    orig = pre_picks._fetch_racelist
    pre_picks._fetch_racelist = lambda rk: None if rk.endswith("_2") else [{"race_key": rk, "lane": 1}]
    try:
        rows, failed = pre_picks.fetch_racelists(["d_01_1", "d_01_2", "d_01_3"])
    finally:
        pre_picks._fetch_racelist = orig
    assert list(rows["race_key"]) == ["d_01_1", "d_01_3"] and failed == ["d_01_2"]


def test_polite_interval_with_threads():
    """並べて取っても、リクエストの開始間隔は3.4秒以上のまま（polite_http の共有ロック）。"""
    import time
    from concurrent.futures import ThreadPoolExecutor

    import polite_http

    starts = []
    orig = polite_http.MIN_INTERVAL
    polite_http.MIN_INTERVAL = 0.2  # テストを速くするため間隔だけ縮める（仕組みは同じ）
    polite_http._last_start = None
    try:
        def go(_):
            polite_http._wait_turn()
            starts.append(time.monotonic())
            time.sleep(0.5)  # 応答待ち
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(go, range(6)))
    finally:
        polite_http.MIN_INTERVAL = orig
    starts.sort()
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.2 - 1e-3


def test_no_money_wording():
    """表示に「儲かる」と書かない。"""
    import inspect

    assert "儲か" not in inspect.getsource(pre_picks_view)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
