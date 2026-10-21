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


def _claude_body():
    parts = {"lead": "こんにちは！今回は展示が良かった1号艇に注目です。",
             "development": "スタートは内の3艇が揃いそうです。1マークは1号艇の先マイが有力で、2号艇の差しが続く形と見ます。"
                            "3着は3号艇と4号艇の争いに期待します。",
             "ticket_comment": "本線は1-2-3 を中心に、抑えで 2-1-3 も押さえます。"}
    return na.compose(parts, TICKETS)


def test_split_article_free_has_no_tickets():
    free, paid = na.split_article(_claude_body(), "桐生 5R", "15:30")
    assert free.startswith("【桐生 5R｜締切 15:30】\n\nこんにちは！")
    assert "■ 展示後の予想展開\nスタートは内の3艇が揃いそうです。" in free
    assert "1マーク" not in free and "1-2-3" not in free and "買い目】" not in free
    assert free.endswith(na.PAYWALL_LEAD)
    assert paid.startswith("■ 展示後の予想展開（続き）\n1マークは1号艇の先マイ")
    assert "■ 買い目の考え方" in paid and "【本線買い目】\n1-2-34" in paid and "保証するものではありません" in paid
    # 段落は空行1つで区切る（noteで段落の境目になる）
    assert "\n\n\n" not in free and "\n\n\n" not in paid


def test_split_article_template_and_combo_in_lead():
    free, paid = na.split_article(na.template_article(TICKETS), "桐生 5R", "15:30")
    assert "1-2-3" not in free and "1-2-34" not in free
    assert paid == na.template_article(TICKETS)
    body = "本命は 1-2-3 です。\n\n" + na.template_article(TICKETS)
    free, paid = na.split_article(body, "桐生 5R")
    assert "1-2-3" not in free and paid.startswith("本命は 1-2-3 です。")
    # 予想展開が1文だけなら、無料に出さない
    body = "見どころです。\n\n■ 展示後の予想展開\n1号艇の逃げが有力です。\n\n" + na.template_article(TICKETS)
    free, paid = na.split_article(body)
    assert "逃げ" not in free and paid.startswith("■ 展示後の予想展開\n1号艇の逃げ")


# ---------------------------------------------------------------
# 中穴〜万舟狙いの買い目を載せる（万舟チャンスのレース）
# ---------------------------------------------------------------
MARK = {"race_key": "20261021_01_5", "aim_source": "predict", "aim_odds_at": "2026-10-21T05:40:00Z",
        "aim_tickets": [{"combo": "2-1-3", "tier": "中穴", "odds": 45.0, "prob": 0.03},
                        {"combo": "2-1-4", "tier": "中穴", "odds": 60.0, "prob": 0.02},
                        {"combo": "4-5-6", "tier": "万舟", "odds": 300.0, "prob": 0.01}]}


def test_aim_option_and_ticket_block():
    aim = na.aim_option(MARK)
    assert aim == [{"combo": "2-1-3", "group": "中穴"}, {"combo": "2-1-4", "group": "中穴"},
                   {"combo": "4-5-6", "group": "万舟"}]
    assert na.aim_option({"aim_tickets": None}) is None and na.aim_option(None) is None
    assert na.is_aim(aim) and not na.is_aim(TICKETS)
    body = na.template_article(aim)
    assert "【中穴（オッズ30〜99倍）】\n2-1-34" in body and "【万舟（オッズ100倍以上）】\n4-5-6" in body
    assert "本線" not in body and "1号艇の1着を外し" in body


def test_aim_brief_and_split():
    f = _final()
    aim = na.aim_option(MARK)
    brief = na.build_brief(f, f, aim, na.p_first_map(f), build_visual_rows(f, None, top_combo="2-1-3"),
                           "桐生 5R", "14:50", "aim")
    assert brief["載せる買い目"] == {"中穴（オッズ30〜99倍）": ["2-1-3", "2-1-4"], "万舟（オッズ100倍以上）": ["4-5-6"],
                                "予想の種類": "中穴〜万舟狙いの買い目"}
    assert "中穴〜万舟狙いの買い目" in na.SYSTEM_PROMPT
    parts = {"lead": "こんにちは！荒れやすいレースです。",
             "development": "スタートは外が速そうです。2号艇の差しが届く展開に期待します。",
             "ticket_comment": "中穴は 2-1-3 から、万舟は 4-5-6 を狙います。"}
    body = na.compose(parts, aim)
    free, paid = na.split_article(body, "桐生 5R", "14:50")
    assert "2-1-3" not in free and "4-5-6" not in free
    assert free.endswith(na.PAYWALL_LEAD_AIM) and "【中穴" in paid and "【万舟" in paid
    # 普段の記事の案内は今までどおり
    assert na.split_article(na.template_article(TICKETS), "桐生 5R", "14:50")[0].endswith(na.PAYWALL_LEAD)


def test_aim_card_renders_png():
    f = _final()
    png = pick_card.render_card("桐生", 5, "14:50", "2026/10/21", na.aim_option(MARK), na.p_first_map(f),
                                build_visual_rows(f, None, top_combo="2-1-3"), "中穴〜万舟狙いの買い目")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10000


def test_threads_aim_style():
    from threads_poster import build_post_text

    t = build_post_text("10/21", "桐生", 5, final=_final(), publication_type="PAID", style="aim")
    assert "万舟チャンス" in t and "中穴〜万舟狙い" in t and "◎" not in t and "noteから" in t
    n = build_post_text("10/21", "桐生", 5, final=_final(), publication_type="PAID")
    assert "万舟チャンス" not in n and "展示後予想" in n


def _note_app():
    import datetime as dt

    import pandas as pd
    import streamlit as st

    import note_view

    note_view._race_flags = lambda ctx, d: (True, False)
    note_view._manshu_mark = lambda ctx, d: st.session_state.get("mark")
    tickets = pd.DataFrame({"combo": ["1-2-3", "1-3-2"], "group": ["本線", "抑え"]})
    final = pd.DataFrame({"lane": range(1, 7), "racer_name": list("ABCDEF"),
                          "p_first": [.5, .15, .12, .1, .08, .05], "avg_st": [.15] * 6, "exhibition_st": [.1] * 6})
    note_view.render("20991231_01_5", dt.date(2099, 12, 31), "桐生", 5, "23:59", final, tickets, final)


def test_note_view_aim_option():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_note_app, default_timeout=60)
    at.run()
    assert not at.exception
    assert at.radio[0].options == ["モデルのみ予想"]  # 万舟チャンスでなければ今までどおり

    at = AppTest.from_function(_note_app, default_timeout=60)
    at.session_state["mark"] = MARK
    at.run()
    assert at.radio[0].options == ["モデルのみ予想", "中穴〜万舟狙いの買い目"]
    at.radio[0].set_value("aim").run()
    assert not at.exception
    body = at.text_area[0].value
    assert "【中穴（オッズ30〜99倍）】" in body and "【本線買い目】" not in body
    assert any("予想タブで予想した 14:40" in c.value for c in at.caption)

    # 万舟チャンスだが買い目をまだ作っていない
    at = AppTest.from_function(_note_app, default_timeout=60)
    at.session_state["mark"] = {**MARK, "aim_tickets": None}
    at.run()
    assert at.radio[0].options == ["モデルのみ予想"]
    assert any("万舟狙いの買い目を作ると選べます" in c.value for c in at.caption)
