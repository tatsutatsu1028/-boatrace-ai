"""スレッズのインサイト集計（threads_insights.py）の、Supabase・API を使わない部分のテスト。"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import threads_insights as ti  # noqa: E402
import threads_poster as tp  # noqa: E402

JST = ti.JST
CODES = {"桐生": "01", "住之江": "12"}


def test_parse_ts():
    ts = ti.parse_ts("2026-10-29T00:00:12+0000")
    assert ts.astimezone(JST).strftime("%Y-%m-%d %H:%M") == "2026-10-29 09:00"
    assert ti.parse_ts("") is None


def test_metric_value_values_and_total():
    data = {"data": [
        {"name": "views", "period": "lifetime", "values": [{"value": 120}]},
        {"name": "likes", "values": [{"value": 3}, {"value": 2}]},
        {"name": "followers_count", "total_value": {"value": 456}},
        {"name": "shares", "values": []},
    ]}
    assert ti.parse_metrics(data) == {"views": 120, "likes": 5, "followers_count": 456, "shares": None}


def test_known_posts_links_each_table():
    auto = [{"race_date": "2026-10-29", "posts": [
        {"post_id": "m1", "race_key": "20261029_01_5", "venue": "桐生", "race_no": 5},
        {"post_id": "m2", "race_key": "20261029_12_8", "venue": "住之江", "race_no": 8},
        {"post_id": None, "race_key": "20261029_12_9"},
    ]}]
    results = [
        {"race_date": "2026-10-29", "item": "20261029_01_5", "post_index": 1, "race_key": "20261029_01_5",
         "venue": "桐生", "race_no": 5, "post_id": "r1"},
        {"race_date": "2026-10-29", "item": "summary", "post_id": "rs"},
    ]
    races = [{"race_key": "20261029_12_3", "post_id": "p1", "style": "aim"}]
    recorded = [{"post_id": "s1", "kind": "summary", "source": "app", "race_date": "2026-10-29"},
                {"post_id": "x", "kind": "manual", "source": "none"}]
    k = ti.known_posts(auto, results, races, recorded)
    assert k["m2"] == {"kind": "morning", "source": "auto_posts", "post_index": 2, "race_key": "20261029_12_8",
                       "race_date": "2026-10-29", "venue": "住之江", "race_no": 8}
    assert k["r1"]["kind"] == "result" and k["r1"]["post_index"] == 1
    assert k["rs"] == {"kind": "result_summary", "source": "result_posts", "race_date": "2026-10-29"}
    assert k["p1"]["kind"] == "race" and k["p1"]["style"] == "aim" and k["p1"]["race_date"] == "2026-10-29"
    assert k["s1"]["kind"] == "summary"
    # 前の回に「手動」と推定しただけの行は、記録として使わない
    assert "x" not in k


def test_guess_from_text_each_template():
    at = datetime(2026, 10, 29, 15, 0, tzinfo=JST)
    morning = tp.build_morning_pick_text("2026-10-29", {"venue": "桐生", "race_no": 5}, 1, 3)
    assert ti.guess_from_text(morning, at, CODES)["kind"] == "morning"
    race = tp.build_post_text("2026-10-29", "住之江", 8)
    got = ti.guess_from_text(race, at, CODES)
    assert got["kind"] == "race" and got["race_key"] == "20261029_12_8" and got["style"] == "normal"
    aim = tp.build_post_text("2026-10-29", "桐生", 12, style="aim")
    assert ti.guess_from_text(aim, at, CODES)["style"] == "aim"
    summary = tp.build_daily_summary_text("2026-10-29", [])
    assert ti.guess_from_text(summary, at, CODES)["kind"] == "summary"
    assert ti.guess_from_text("🏁【結果】10/29 朝の無料3レース（1/3）", at, CODES)["kind"] == "result"
    assert ti.guess_from_text("📋 10/29 本日の厳選3レースの結果まとめ", at, CODES)["kind"] == "result_summary"
    assert ti.guess_from_text("今日もよろしくお願いします", at, CODES) is None


def test_build_post_row_manual_and_known():
    now = datetime(2026, 10, 29, 22, 0, tzinfo=JST)
    api = {"id": "9", "text": "こんにちは" * 30, "timestamp": "2026-10-29T03:00:00+0000",
           "media_type": "TEXT_POST", "is_reply": True, "permalink": "https://www.threads.net/@x/post/abc"}
    row = ti.build_post_row(api, {}, CODES, now)
    assert row["kind"] == "manual_reply" and row["source"] == "none"
    assert row["posted_date"] == "2026-10-29" and len(row["text_head"]) == tp.TEXT_HEAD_LEN
    assert row["text_length"] == 150
    row = ti.build_post_row({**api, "id": "m1"}, {"m1": {"kind": "morning", "post_index": 1}}, CODES, now)
    assert row["kind"] == "morning" and row["post_index"] == 1
    # 一度にまとめて書くので、どの行も同じ列を持つ
    assert set(row) == set(ti.build_post_row(api, {}, CODES, now))


def test_recent_post_ids_only_within_14_days():
    now = datetime(2026, 10, 29, 22, 0, tzinfo=JST)
    rows = [{"post_id": "new", "posted_at": (now - timedelta(days=13)).isoformat()},
            {"post_id": "old", "posted_at": (now - timedelta(days=15)).isoformat()},
            {"post_id": "none", "posted_at": None}]
    assert [pid for pid, _ in ti.recent_post_ids(rows, now)] == ["new"]


def test_should_collect_once_a_day():
    early = datetime(2026, 10, 29, 21, 0, tzinfo=JST)
    late = datetime(2026, 10, 29, 21, 32, tzinfo=JST)
    assert ti.should_collect(early, None)[0] is False
    assert ti.should_collect(early, None, force=True)[0] is True
    assert ti.should_collect(late, None)[0] is True
    assert ti.should_collect(late, {"status": "done", "attempts": 1})[0] is False
    assert ti.should_collect(late, {"status": "failed", "attempts": 1})[0] is True
    assert ti.should_collect(late, {"status": "failed", "attempts": 3})[0] is False
    running = {"status": "running", "attempts": 1, "attempted_at": (late - timedelta(minutes=5)).isoformat()}
    assert ti.should_collect(late, running)[0] is False
    stale = {**running, "attempted_at": (late - timedelta(minutes=30)).isoformat()}
    assert ti.should_collect(late, stale)[0] is True


def test_list_own_posts_stops_at_since(monkeypatch):
    pages = [
        {"data": [{"id": "1", "timestamp": "2026-10-29T00:00:00+0000"},
                  {"id": "2", "timestamp": "2026-10-20T00:00:00+0000"}],
         "paging": {"cursors": {"after": "A"}, "next": "https://next"}},
        {"data": [{"id": "3", "timestamp": "2026-10-01T00:00:00+0000"}], "paging": {}},
    ]
    calls = []

    def fake_api(path, token, params=None):
        calls.append(dict(params or {}))
        return pages[len(calls) - 1]

    monkeypatch.setattr(ti, "_api", fake_api)
    since = datetime(2026, 10, 15, tzinfo=JST)
    assert [p["id"] for p in ti.list_own_posts("t", since=since, sleep=lambda s: None)] == ["1", "2"]
    calls.clear()
    assert [p["id"] for p in ti.list_own_posts("t", since=datetime(2026, 10, 25, tzinfo=JST),
                                               sleep=lambda s: None)] == ["1"]
    assert len(calls) == 1
    calls.clear()
    assert [p["id"] for p in ti.list_own_posts("t", since=None, sleep=lambda s: None)] == ["1", "2", "3"]
    assert calls[1]["after"] == "A"


def test_fetch_post_metrics_retries_without_shares(monkeypatch):
    def fake_api(path, token, params=None):
        if "shares" in params["metric"]:
            raise RuntimeError("HTTP 400")
        return {"data": [{"name": m, "values": [{"value": 1}]} for m in params["metric"].split(",")]}

    monkeypatch.setattr(ti, "_api", fake_api)
    got = ti.fetch_post_metrics("1", "t")
    assert got["views"] == 1 and got["shares"] is None


def test_record_post_writes_row_and_never_raises(monkeypatch):
    sent = []

    class R:
        status_code = 201

    def post(url, headers=None, json=None, timeout=None):
        sent.append((url, headers, json))
        return R()

    monkeypatch.setattr(tp.requests, "post", post)
    assert tp.record_post("https://sb", "key", "123", "summary", "📊 本文", race_date="2026-10-29") is True
    url, headers, row = sent[0]
    assert url.endswith("/rest/v1/threads_posts") and "merge-duplicates" in headers["Prefer"]
    assert row["kind"] == "summary" and row["source"] == "app" and row["race_date"] == "2026-10-29"

    def boom(*a, **kw):
        raise tp.requests.ConnectionError("x")

    monkeypatch.setattr(tp.requests, "post", boom)
    assert tp.record_post("https://sb", "key", "123", "summary") is False
    assert tp.record_post("", "key", "123", "summary") is False
