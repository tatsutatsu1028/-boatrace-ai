"""
スレッズの結果の自動投稿（threads_results.py）の、Supabase やスレッズに繋がなくても確かめられる部分。

  python -m pytest tests/test_threads_results.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threads_poster as tp  # noqa: E402
import threads_results as tr  # noqa: E402

ISO = "2026-10-16"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=tr.JST)


def _post(rno=5, combos=("1-2-3", "1-2-4", "1-3-4")):
    return {"race_key": f"20261016_01_{rno:02d}", "venue": "桐生", "race_no": rno, "deadline": "11:00",
            "tickets": [{"combo": c, "group": "本線"} for c in combos], "post_id": f"p{rno}"}


def test_judge_hit_miss_and_void():
    hit = tr.judge(_post(), {"status": "settled", "trifecta_actual": "1-2-4", "trifecta_payout": 1230})
    assert hit["outcome"] == "hit" and hit["hit_combos"] == ["1-2-4"] and hit["trifecta_payout"] == 1230
    miss = tr.judge(_post(), {"status": "settled", "trifecta_actual": "2-1-3", "trifecta_payout": 5000})
    assert miss["outcome"] == "miss" and miss["hit_combos"] == []
    # 同着: どちらかに買い目があれば的中
    tie = tr.judge(_post(), {"status": "settled", "trifecta_actual": "2-1-3 1-3-4", "trifecta_payout": 800})
    assert tie["outcome"] == "hit" and tie["hit_combos"] == ["1-3-4"]
    assert tr.judge(_post(), {"status": "cancelled"})["outcome"] == "cancelled"
    assert tr.judge(_post(), {"status": "refunded"})["outcome"] == "refunded"
    assert tr.judge(_post(), None) is None


def test_pick_result_reads_list_columns():
    assert tr.pick_result({"result_at": None}) is None
    assert tr.pick_result({"result_at": "x", "result_status": "void"}) == {"status": "cancelled"}
    r = tr.pick_result({"result_at": "x", "result_status": "settled", "trifecta_actual": "1-2-3",
                        "trifecta_payout": 900})
    assert r["status"] == "settled" and r["trifecta_actual"] == "1-2-3"


def test_race_text_hit_miss_void_has_no_money():
    hit = tr.judge(_post(), {"status": "settled", "trifecta_actual": "1-2-4", "trifecta_payout": 12340})
    t = tr.build_race_text(ISO, hit, 1, 3)
    assert "桐生 5R" in t and "1-2-4" in t and "12,340円" in t and "的中" in t and "（1/3）" in t
    assert "収支" not in t and "購入" not in t and "不的中" not in t
    miss = tr.judge(_post(), {"status": "settled", "trifecta_actual": "6-5-4", "trifecta_payout": 99999})
    t = tr.build_race_text(ISO, miss, 2, 3)
    assert t.rstrip().endswith("不的中") and "6-5-4" in t
    t = tr.build_race_text(ISO, tr.judge(_post(), {"status": "cancelled"}), 3, 3)
    assert "中止" in t and "的中" not in t
    t = tr.build_race_text(ISO, tr.judge(_post(), {"status": "refunded"}), 3, 3)
    assert "返還" in t
    assert len(t) <= tp.TEXT_LIMIT


def test_summary_counts_and_text():
    rs = [tr.judge(_post(1), {"status": "settled", "trifecta_actual": "1-2-3", "trifecta_payout": 500}),
          tr.judge(_post(2), {"status": "settled", "trifecta_actual": "6-5-4", "trifecta_payout": 9000}),
          tr.judge(_post(3), {"status": "cancelled"})]
    assert tr.summary_counts(rs) == (1, 2, 1)
    t = tr.build_summary_text(ISO, rs)
    assert "本日の厳選3レースの結果まとめ" in t and "2レース中1レース的中" in t and "中止 1レース" in t
    assert "桐生 3R　中止" in t
    rs = rs[:2] + [tr.judge(_post(3), {"status": "settled", "trifecta_actual": "1-2-4", "trifecta_payout": 700})]
    t = tr.build_summary_text(ISO, rs)
    assert "3レース中2レース的中" in t and "中止" not in t


def test_writer_used_and_falls_back():
    r = tr.judge(_post(), {"status": "settled", "trifecta_actual": "1-2-4", "trifecta_payout": 1230})
    data = {"result": r, "index": 1, "total": 3}
    assert tr.make_text(ISO, "race", data, writer=lambda d, k, x: f"{k}:{x['index']}") == "race:1"

    def boom(*a):
        raise RuntimeError("x")

    for w in (boom, lambda *a: "", lambda *a: "あ" * (tp.TEXT_LIMIT + 1)):
        assert tr.make_text(ISO, "race", data, writer=w) == tr.build_race_text(ISO, r, 1, 3)


def test_morning_posts_only_when_all_posted():
    posts = [_post(1), _post(2), _post(3)]
    assert tr.morning_posts({"status": "posted", "posts": posts}) == posts
    assert tr.morning_posts({"status": "partial", "posts": posts}) is None
    assert tr.morning_posts({"status": "skipped", "posts": None}) is None
    assert tr.morning_posts(None) is None
    broken = [_post(1), {**_post(2), "post_id": None}, _post(3)]
    assert tr.morning_posts({"status": "posted", "posts": broken}) is None


def test_can_try_retries_then_stops_and_never_reposts():
    assert tr.can_try(None, NOW) == (True, None)
    assert tr.can_try({"status": "waiting", "attempts": 0}, NOW) == (True, None)
    assert tr.can_try({"status": "failed", "attempts": 2}, NOW) == (True, None)
    ok, why = tr.can_try({"status": "failed", "attempts": 3}, NOW)
    assert not ok and "3回" in why
    assert tr.can_try({"status": "posted", "attempts": 1}, NOW) == (False, None)
    # 投稿中（今まさに）は待つ。古いままなら二重投稿を防ぐため止める
    fresh = {"status": "posting", "attempts": 1, "attempted_at": (NOW - timedelta(minutes=1)).isoformat()}
    assert tr.can_try(fresh, NOW) == (False, None)
    stale = {"status": "posting", "attempts": 1, "attempted_at": (NOW - timedelta(minutes=30)).isoformat()}
    ok, why = tr.can_try(stale, NOW)
    assert not ok and "二重投稿" in why


def test_is_final_and_from_record():
    assert not tr.is_final(None, NOW)
    assert not tr.is_final({"status": "waiting"}, NOW)
    assert tr.is_final({"status": "posted", "outcome": "hit"}, NOW)
    assert not tr.is_final({"status": "failed", "attempts": 1, "outcome": "miss"}, NOW)
    assert tr.is_final({"status": "failed", "attempts": 3, "outcome": "miss"}, NOW)
    # 結果が分からずにあきらめたレースは終わりにしない（まとめを出さない）
    assert not tr.is_final({"status": "failed", "attempts": 3, "outcome": None}, NOW)
    rec = {"race_key": "k", "venue": "桐生", "race_no": 5, "outcome": "refunded", "hit_combos": None}
    assert tr.from_record(rec)["outcome"] == "refunded"
    assert tr.from_record({"status": "waiting"}) is None


def test_page_void_status():
    assert tr.page_void_status("<div>レース中止</div>") == "cancelled"
    assert tr.page_void_status("<td>3連単</td><td>特払い</td><td>¥70</td>") == "refunded"
    assert tr.page_void_status("<td>3連単</td><td>1-2-3</td><td>¥1,230</td>") is None


def test_next_tick_matches_track_odds_schedule():
    t = datetime(2026, 10, 16, 11, 10, 30, tzinfo=tr.JST)
    assert tr.next_tick(t).strftime("%H:%M") == "11:12"
    assert tr.next_tick(datetime(2026, 10, 16, 11, 12, tzinfo=tr.JST)).strftime("%H:%M") == "11:17"
