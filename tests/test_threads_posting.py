"""
スレッズの朝の自動投稿（threads_morning.py）と、夜のまとめ（threads_poster.py）の、
Supabase やスレッズに繋がなくても確かめられる部分。

  python -m pytest tests/test_threads_posting.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threads_morning as tm  # noqa: E402
import threads_poster as tp  # noqa: E402

ISO = "2026-10-16"


def _pick(rank, hp, deadline, rno=None, stake=200):
    return {
        "race_key": f"20261016_01_{rno or rank}", "race_date": ISO, "jcd": "01", "venue": "桐生",
        "race_no": rno or rank, "deadline": deadline, "pick_rank": rank, "model_version": "pre-v1",
        "hit_probability": hp, "favorite_lane": 1, "favorite_name": "山田太郎",
        "favorite_prob": 0.6, "p_first": [0.6, 0.1, 0.1, 0.1, 0.05, 0.05],
        "tickets": [{"combo": "1-2-3", "group": "本線", "prob": 0.1, "stake": stake},
                    {"combo": "1-2-4", "group": "本線", "prob": 0.08, "stake": stake},
                    {"combo": "1-3-2", "group": "抑え", "prob": 0.05, "stake": 0},
                    {"combo": "1-3-4", "group": "穴", "prob": 0.04, "stake": stake}],
    }


def test_select_skips_close_deadlines_and_promotes_next():
    now = datetime(2026, 10, 16, 9, 0, tzinfo=tm.JST)
    picks = [_pick(1, 0.7, "09:20"), _pick(2, 0.6, "09:30"), _pick(3, 0.5, "12:00"),
             _pick(4, 0.5, "10:00"), _pick(5, 0.4, "15:00"), _pick(6, 0.3, "16:00")]
    chosen = tm.select_for_post(picks, now)
    # 09:20 は30分未満なので飛ばす。09:30 はちょうど30分なので入る。同じ確率なら締切が早い順
    assert [p["pick_rank"] for p in chosen] == [2, 4, 3]


def test_select_returns_fewer_when_not_enough():
    now = datetime(2026, 10, 16, 14, 50, tzinfo=tm.JST)
    picks = [_pick(1, 0.7, "15:00"), _pick(2, 0.6, "15:30"), _pick(3, 0.5, "16:00")]
    assert len(tm.select_for_post(picks, now)) == 2


def test_validate_ok_and_problems():
    picks = [_pick(i, 0.5, "12:00") for i in range(1, 11)]
    run = {"picked_count": 10}
    assert tm.validate_picks(picks, run, ISO) == []
    assert tm.validate_picks(picks, None, ISO)
    bad = [dict(p) for p in picks]
    bad[0]["p_first"] = [0.9, 0.9, 0, 0, 0, 0]
    bad[1]["tickets"] = [{"combo": "1-1-2", "group": "本線", "stake": 100}]
    bad[2]["favorite_lane"] = 3
    bad[3]["hit_probability"] = None
    bad[4]["tickets"] = [{"combo": "1-2-3", "group": "本線", "stake": 0}]
    probs = tm.validate_picks(bad, run, ISO)
    assert len(probs) == 5, probs


def test_texts_contain_race_deadline_favorite_tickets():
    p = _pick(1, 0.5, "10:12", rno=3)
    texts = tm.build_texts(ISO, [p, _pick(2, 0.5, "11:00"), _pick(3, 0.5, "12:00")])
    t = texts[0]
    assert "展示前" in t and "桐生 3R" in t and "10:12" in t and "1号艇 山田太郎" in t
    assert "1-2-34" in t and "1-3-4" in t
    assert "1-3-2" not in t  # 0円の候補は載せない
    assert "（1/3）" in t and "（3/3）" in texts[2]
    assert all(len(x) <= tp.TEXT_LIMIT for x in texts)


def test_writer_falls_back_to_template():
    picks = [_pick(1, 0.5, "10:00"), _pick(2, 0.5, "11:00")]

    def boom(*a):
        raise RuntimeError("x")

    assert tm.build_texts(ISO, picks, writer=boom) == tm.build_texts(ISO, picks)
    assert tm.build_texts(ISO, picks, writer=lambda *a: "あ" * 600) == tm.build_texts(ISO, picks)
    assert tm.build_texts(ISO, picks, writer=lambda d, p, i, n: f"本文{i}") == ["本文1", "本文2"]


def _rec(section, key, tickets, actual, legacy=None):
    return tp.judge_record({"section": section, "race_key": key, "venue": "桐生", "race_no": int(key[-1]),
                            "tickets": tickets, "trifecta_actual": actual, "legacy": legacy or {}})


def test_note_hit_uses_article_tickets():
    r = _rec("note", "k_1", ["1-2-3", "1-3-2"], "1-3-2")
    assert r["settled"] and r["hit"] and not r["hit_top_ticket"]
    r = _rec("note", "k_2", ["1-2-3"], "2-1-3", legacy={"candidate_hit": True})
    assert r["settled"] and not r["hit"]
    # 買い目を記録していない古い行は今までどおり
    r = _rec("note", "k_3", [], "2-1-3", legacy={"candidate_hit": True})
    assert r["hit"]
    r = _rec("note", "k_4", ["1-2-3"], "")
    assert not r["settled"]
    tp.apply_actuals([r], {"k_4": "1-2-3"})
    assert r["settled"] and r["hit"] and r["hit_top_ticket"]


def test_summary_has_two_sections_and_closing():
    morning = [_rec("morning", "m_1", ["1-2-3"], "1-2-3"), _rec("morning", "m_2", ["1-2-3"], "3-2-1"),
               _rec("morning", "m_3", ["1-2-3"], "")]
    note = [_rec("note", "n_4", ["4-1-2"], "4-1-2")]
    text = tp.build_daily_summary_text(ISO, note, payouts={"n_4": 5230}, morning_records=morning)
    assert text.index("朝の無料3レース") < text.index("noteのレース")
    assert "⭕ 桐生1R 1-2-3" in text and "❌ 桐生2R 3-2-1" in text and "⏳ 桐生3R 結果待ち" in text
    assert "⭕ 桐生4R 4-1-2（5,230円）" in text
    assert "明日の予想もお楽しみに✨" in text  # 確定3レース中2的中
    text = tp.build_daily_summary_text(ISO, [], morning_records=morning[:2])
    assert "五分五分でした" in text and "本日の記事はありませんでした" in text
    text = tp.build_daily_summary_text(ISO, note[:0], morning_records=[morning[1]])
    assert "もっと予想精度上げれる様に頑張ります💪" in text
    assert "本日の発信はありませんでした" in tp.build_daily_summary_text(ISO, [], morning_records=[])


def test_compact_and_split():
    main, cover = tp.split_main_cover([{"combo": "1-2-3", "group": "本線"}, {"combo": "1-2-4", "group": "本線"},
                                       {"combo": "2-1-3", "group": "穴"}])
    assert tp.compact_combos(main) == ["1-2-34"] and cover == ["2-1-3"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")


class _FakeDB:
    def __init__(self, picks, enabled=True):
        self.picks, self.enabled, self.rows = picks, enabled, {}

    def __call__(self, method, path, prefer=None, **kw):
        class R:
            def __init__(self, data):
                self._d = data

            def json(self):
                return self._d

        if method == "GET" and path.startswith("threads_auto_posts"):
            return R([self.rows[ISO]] if ISO in self.rows else [])
        if method == "GET" and path.startswith("app_settings"):
            return R([{"threads_auto_post_enabled": self.enabled}])
        if method == "GET" and path.startswith("pre_exhibition_runs"):
            return R([{"picked_count": len(self.picks)}])
        if method == "GET" and path.startswith("pre_exhibition_picks"):
            return R(self.picks)
        if method == "GET" and path.startswith("note_publications"):
            return R([{"race_key": self.picks[0]["race_key"]}])
        if method == "POST" and path.startswith("threads_auto_posts"):
            row = dict(self.rows.get(ISO, {}))
            row.update(kw["json"])
            self.rows[ISO] = row
            return R([])
        raise AssertionError(path)


def _setup(monkeypatch, fail_at=None, enabled=True, image=("https://x/img.png", None), image_fails=False):
    picks = [_pick(i, 0.9 - i / 20, "23:00", rno=i) for i in range(1, 11)]
    db = _FakeDB(picks, enabled)
    calls = []

    def post(uid, token, text, reply_to_id=None):
        calls.append(reply_to_id)
        if fail_at is not None and len(calls) >= fail_at:  # この回から先（やり直しも）失敗
            raise RuntimeError("boom")
        return f"id{len(calls)}"

    def post_image(uid, token, text, image_url, reply_to_id=None):
        calls.append(("image", image_url))
        if image_fails:
            raise RuntimeError("image boom")
        return "img1"

    monkeypatch.setattr(tm, "prepare_image", lambda iso, chosen: image)
    monkeypatch.setattr(tm.tp, "post_image", post_image)
    monkeypatch.setattr(tm, "_sb", db)
    monkeypatch.setattr(tm, "_load_threads_config", lambda: {"user_id": "u", "access_token": "t"})
    monkeypatch.setattr(tm.tp, "post_text", post)
    monkeypatch.setattr(tm.time, "sleep", lambda s: None)
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 9, 0, tzinfo=tm.JST)

    monkeypatch.setattr(tm, "datetime", _Now)
    return db, calls


def test_run_posts_thread_once(monkeypatch):
    db, calls = _setup(monkeypatch)
    tm.run("20261016")
    row = db.rows[ISO]
    assert row["status"] == "posted" and calls == [("image", "https://x/img.png"), "img1", "id2"]
    assert row["image_url"] == "https://x/img.png" and row["posts"][0]["image_url"] == "https://x/img.png"
    assert [p["race_no"] for p in row["posts"]] == [1, 2, 3]
    assert row["note_overlap"] == [row["posts"][0]["race_key"]] and row["ticket_source"] == "model"
    tm.run("20261016")
    tm.run("20261016", retry=True)
    assert len(calls) == 3  # 同じ日には2回投稿しない


def test_run_partial_failure_and_off(monkeypatch):
    db, calls = _setup(monkeypatch, fail_at=2)
    tm.run("20261016")
    row = db.rows[ISO]
    assert row["status"] == "partial" and row["posts"][0]["post_id"] and row["posts"][1]["error"]
    db, calls = _setup(monkeypatch, enabled=False)
    tm.run("20261016")
    assert db.rows[ISO]["status"] == "skipped" and not calls


def test_image_failures_fall_back_to_text(monkeypatch):
    # 画像を用意できなかった日
    db, calls = _setup(monkeypatch, image=(None, "画像を用意できませんでした: X"))
    tm.run("20261016")
    row = db.rows[ISO]
    assert row["status"] == "posted" and calls == [None, "id1", "id2"]
    assert row["image_url"] is None and "用意できません" in row["image_error"]
    # 画像付きの投稿が失敗した日は、画像なしで投稿し直す
    db, calls = _setup(monkeypatch, image_fails=True)
    tm.run("20261016")
    row = db.rows[ISO]
    assert row["status"] == "posted" and calls[0][0] == "image" and calls[1] is None
    assert row["posts"][0]["image_url"] is None and "画像なしで投稿" in row["image_error"]


def test_morning_image_renders():
    png = tm.make_image(ISO, [_pick(1, 0.5, "10:12", rno=3), _pick(2, 0.5, "11:00"), _pick(3, 0.5, "12:00")])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10000


def test_wait_container(monkeypatch):
    seq = iter([{"status": "IN_PROGRESS"}, {"status": "FINISHED"}])

    class R:
        status_code = 200

        def __init__(self):
            self._d = next(seq)

        def json(self):
            return self._d

    monkeypatch.setattr(tp.requests, "get", lambda *a, **k: R())
    tp.wait_container("c", "t", sleep=lambda s: None)
    seq = iter([{"status": "ERROR", "error_message": "bad"}])
    try:
        tp.wait_container("c", "t", sleep=lambda s: None)
        raise AssertionError("例外が出ない")
    except RuntimeError as e:
        assert "bad" in str(e)
