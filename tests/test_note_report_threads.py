"""
結果報告記事の Threads 自動投稿（note_report_threads.py）の、Claude・Threads・Supabase に繋がなくても確かめられる部分。

  python -m pytest tests/test_note_report_threads.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import note_report_threads as nrt  # noqa: E402

URL = "https://note.com/tenji_kara/n/n1a2b3c4d5e6f"


def _rec(venue, rno, hit=None, actual="1-2-3"):
    r = {"race_key": f"20261107_04_{rno}", "venue": venue, "race_no": rno, "settled": hit is not None,
         "hit": bool(hit), "trifecta_actual": actual if hit is not None else ""}
    return r


RECS = [_rec("常滑", 12, True), _rec("平和島", 5, False, "3-1-2"), _rec("鳴門", 9)]
PAY = {"20261107_04_12": 1230}


def test_text_has_results_and_url_without_claude():
    text, res = nrt.build_text("2026-11-07", RECS, PAY, URL, api_key="")
    assert res["status"] == "error"
    assert text.startswith(nrt.DEFAULT_INTRO)
    assert "📊 11/7のnote予想の結果" in text and "3レースのうち、結果確定2レース中、1レース的中🎯" in text
    assert "⭕ 常滑12R 1-2-3（1,230円）" in text and "❌ 平和島5R 3-1-2" in text and "⏳ 鳴門9R 結果待ち" in text
    assert text.endswith(f"{nrt.NOTE_LEAD}\n{URL}") and len(text) <= 500


def test_claude_words_used_and_checked(monkeypatch):
    monkeypatch.setattr(nrt, "write_words", lambda k, f, m=None: {"status": "ok", "intro": "今日もありがとうございました😊",
                                                                    "closing": "明日も展示から読みます！"})
    text, _ = nrt.build_text("2026-11-07", RECS, PAY, URL, api_key="k")
    assert text.startswith("今日もありがとうございました😊") and "明日も展示から読みます！" in text
    # 数字・使わない言葉・長すぎる文は使わない
    assert nrt._clean_part("3レース的中でした", 80) is None
    assert nrt._clean_part("絶対また当てます", 80) is None
    assert nrt._clean_part("あ" * 81, 80) is None
    assert nrt._clean_part(" よい一日でした ", 80) == "よい一日でした"


def test_fits_500_chars_dropping_misses_first():
    many = [_rec("常滑", 1, True)] + [_rec("とても長い会場名の場", n, False, "6-5-4") for n in range(2, 40)]
    text = nrt.compose("あいさつ" * 10, nrt.result_lines("2026-11-07", many, {}), "しめ" * 10, URL)
    assert len(text) <= 500 and text.endswith(URL)
    assert "⭕ 常滑1R" in text and "ほか" in text and "📊 11/7のnote予想の結果" in text


def test_no_records():
    text = nrt.compose("a", nrt.result_lines("2026-11-07", [], {}), "b", URL)
    assert "本日のnote記事はありませんでした。" in text


def test_post_uses_existing_threads(monkeypatch):
    import threads_poster as tp
    calls = []
    monkeypatch.setattr(tp, "load_config", lambda u, k: {"user_id": "u", "access_token": "t"})
    monkeypatch.setattr(tp, "post_text", lambda u, t, text: calls.append(text) or "P1")
    monkeypatch.setattr(tp, "record_post", lambda u, k, pid, kind, text: calls.append(kind))
    assert nrt.post("url", "key", "本文") == "P1" and calls == ["本文", "summary"]
    monkeypatch.setattr(tp, "load_config", lambda u, k: None)
    try:
        nrt.post("url", "key", "本文")
        raise AssertionError
    except RuntimeError as e:
        assert "連携情報" in str(e)
