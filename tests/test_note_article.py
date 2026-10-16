"""
note の有料記事（note_article.py）と予想カード（pick_card.py）の、API に繋がなくても確かめられる部分。

  python -m pytest tests/test_note_article.py
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import note_article as na  # noqa: E402
import pick_card  # noqa: E402
from race_visuals import build_visual_rows  # noqa: E402

TICKETS = [{"combo": "1-2-3", "group": "本線"}, {"combo": "1-2-4", "group": "本線"},
           {"combo": "1-3-2", "group": "抑え"}, {"combo": "2-1-3", "group": "穴"}]


def _final():
    return pd.DataFrame({"lane": range(1, 7), "racer_name": list("ABCDEF"),
                         "p_first": [.5, .15, .12, .1, .08, .05], "avg_st": [.15] * 6, "exhibition_st": [.1] * 6})


def test_template_is_previous_article_format():
    body = na.template_article(TICKETS)
    assert body.startswith("【本線買い目】\n1-2-34\n\n【抑え買い目】\n1-3-2\n2-1-3")
    assert "的中や利益を保証するものではありません" in body


def test_check_text_rules():
    assert na.check_text("1号艇の逃げに期待します。1-2-3 が本線です。", TICKETS) is None
    assert "絶対" in na.check_text("1号艇が絶対です。", TICKETS)
    assert "鉄板" in na.check_text("鉄板の1号艇。", TICKETS)
    assert "4-5-6" in na.check_text("4-5-6 も面白い。", TICKETS)


def test_compose_appends_selected_tickets():
    body = na.compose({"lead": "こんにちは", "development": "展開", "ticket_comment": "考え方"}, TICKETS)
    assert body.index("■ 展示後の予想展開") < body.index("【本線買い目】")
    assert body.endswith(na.template_article(TICKETS))


def test_avg_option_only_when_claude_was_taken():
    assert na.avg_option(None) is None
    assert na.avg_option({"status": "error"}) is None
    sec = {"status": "ok", "avg_tickets": [{"combo": "1-3-2", "group": "本線", "stake": 0}],
           "avg_p_first": {"1": 0.4, "2": 0.2, "3": 0.2, "4": 0.1, "5": 0.05, "6": 0.05}}
    tickets, probs = na.avg_option(sec)
    assert tickets == [{"combo": "1-3-2", "group": "本線"}] and probs[1] == 0.4


def test_write_without_key_is_error_not_exception():
    out = na.write_with_claude("", "k", {"race": {}}, TICKETS)
    assert out["status"] == "error"


def test_brief_uses_selected_probs():
    f = _final()
    rows = build_visual_rows(f, None, top_combo="1-3-2")
    brief = na.build_brief(f, f, TICKETS, {1: 0.4, 2: 0.2, 3: 0.2, 4: 0.1, 5: 0.05, 6: 0.05}, rows,
                           "桐生 3R", "10:12", "avg")
    assert brief["boats"][0]["1着確率"] == 0.4 and "モデル1着確率" not in brief["boats"][0]
    assert brief["載せる買い目"]["本線"] == ["1-2-3", "1-2-4"]
    assert brief["1マークの想定の並び"].startswith("1-3-2")


def test_card_renders_png():
    f = _final()
    png = pick_card.render_card("桐生", 3, "10:12", "2026/10/16", TICKETS, na.p_first_map(f),
                                build_visual_rows(f, None, top_combo="1-2-3"), "モデルのみ予想")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10000


class _Resp:
    def __init__(self, text, stop="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop
        self.model = "claude-haiku-5-5"
        self.usage = type("U", (), {"input_tokens": 1000, "output_tokens": 500})()


def _fake_anthropic(monkeypatch, text, calls):
    import types

    class Client:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            calls.append(kw)
            return _Resp(text)

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))


def test_claude_ok_and_rules(monkeypatch):
    import json

    calls = []
    good = json.dumps({"lead": "こんにちは！", "development": "1号艇の逃げが有力と見ます。", "ticket_comment": "1-2-3 を本線に。"})
    _fake_anthropic(monkeypatch, good, calls)
    out = na.write_with_claude("key", "k1", {"race": {}}, TICKETS)
    assert out["status"] == "ok" and out["body"].endswith(na.template_article(TICKETS)) and out["cost"]
    assert calls[0]["model"] == "claude-haiku-5-5" and calls[0]["output_config"]["format"]["type"] == "json_schema"
    bad = json.dumps({"lead": "鉄板です", "development": "x", "ticket_comment": "y"})
    _fake_anthropic(monkeypatch, bad, calls)
    out = na.write_with_claude("key", "k2", {"race": {}}, TICKETS)
    assert out["status"] == "error" and "鉄板" in out["error"]
