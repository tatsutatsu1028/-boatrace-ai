"""
note の自動下書き（note_auto.py）と、展示後の自動予想（auto_post_predict.py）への組み込みの、
Supabase・Claude に繋がなくても確かめられる部分。

  python -m pytest tests/test_note_auto.py
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_post_predict as ap  # noqa: E402
import note_article as na  # noqa: E402
import note_auto  # noqa: E402

TICKETS = pd.DataFrame([{"combo": "1-2-3", "group": "本線", "stake": 500}, {"combo": "1-2-4", "group": "本線", "stake": 300},
                        {"combo": "1-3-2", "group": "抑え", "stake": 200}])
T = {"race_key": "20261107_04_5", "race_date": "2026-11-07", "jcd": "04", "venue": "平和島", "race_no": 5,
     "deadline": "11:30", "sources": ["pre"]}
ON = {"note_auto_enabled": True, "note_auto_ticket_source": "model"}


def _final():
    return pd.DataFrame({"lane": range(1, 7), "racer_name": list("ABCDEF"),
                         "p_first": [.5, .15, .12, .1, .08, .05], "avg_st": [.15] * 6, "exhibition_st": [.1] * 6})


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeSb:
    def __init__(self, save_result="created", fail=False):
        self.calls = []
        self.save_result = save_result
        self.fail = fail

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "rpc/note_auto_save_draft":
            if self.fail:
                raise RuntimeError("HTTP 500")
            return _Resp(self.save_result)
        return _Resp(None)

    def saved(self):
        return next(kw["json"] for m, p, kw in self.calls if p == "rpc/note_auto_save_draft")


def _claude_ok(monkeypatch):
    def fake(api_key, race_key, brief, tickets, model=na.ARTICLE_MODEL, force=False):
        parts = {"lead": "こんにちは！平和島5Rの見どころです。",
                 "development": "スタートは内がそろいそうです。1号艇が先に回る展開を本線に見ます。",
                 "ticket_comment": "1-2-3 を本線にしました。"}
        return {"status": "ok", "body": na.compose(parts, tickets), "called_api": False}
    monkeypatch.setattr(na, "write_with_claude", fake)


def test_paywall_mark_between_free_and_paid():
    body = na.compose({"lead": "こんにちは", "development": "スタートは横並び。1号艇が逃げます。",
                       "ticket_comment": "1-2-3 が本線"}, na.tickets_list(TICKETS))
    text = note_auto.with_paywall_mark(body, "平和島 5R", "11:30")
    free, paid = note_auto.split_by_mark(text)
    assert "\n\n【有料ライン】\n\n" in text and text.count(note_auto.PAYWALL_MARK) == 1
    assert free.startswith("【平和島 5R｜締切 11:30】")
    assert "1-2-3" not in free and "【本線買い目】" in paid
    assert note_auto.split_by_mark("目印なし") == ("目印なし", "")


def test_settings_defaults():
    assert note_auto.enabled({}) is False and note_auto.enabled(None) is False
    assert note_auto.ticket_source({}) == "model"
    assert note_auto.ticket_source({"note_auto_ticket_source": "avg"}) == "avg"
    assert note_auto.ticket_source({"note_auto_ticket_source": "aim"}) == "model"


def test_off_writes_nothing():
    sb = FakeSb()
    out = note_auto.generate(sb, T, _final(), TICKETS, settings={"note_auto_enabled": False})
    assert out["status"] == "off" and sb.calls == []


def test_generate_ok_saves_auto_draft(monkeypatch):
    _claude_ok(monkeypatch)
    sb = FakeSb()
    out = note_auto.generate(sb, T, _final(), TICKETS, settings=ON, api_key="x")
    assert out["status"] == "ok" and out["writer"] == "claude" and out["source"] == "model"
    row = sb.saved()
    assert row["p_title"] == "【有料予想】平和島 5R｜締切 11:30" and row["p_price"] == 300
    assert row["p_ticket_source"] == "model" and row["p_article_writer"] == "claude"
    assert [t["combo"] for t in row["p_article_tickets"]] == ["1-2-3", "1-2-4", "1-3-2"]
    assert note_auto.PAYWALL_MARK in row["p_article_text"] and row["p_p1_lane"] == 1


def test_claude_failure_is_template_with_reason():
    sb = FakeSb()
    out = note_auto.generate(sb, T, _final(), TICKETS, settings=ON, api_key="")
    assert out["status"] == "template" and "ANTHROPIC_API_KEY" in out["reason"]
    row = sb.saved()
    assert row["p_article_writer"] == "template" and "【本線買い目】" in row["p_article_text"]


def test_manual_article_not_overwritten(monkeypatch):
    _claude_ok(monkeypatch)
    out = note_auto.generate(FakeSb(save_result="manual"), T, _final(), TICKETS, settings=ON, api_key="x")
    assert out["status"] == "manual"


def test_save_error_is_failed_not_exception(monkeypatch):
    _claude_ok(monkeypatch)
    out = note_auto.generate(FakeSb(fail=True), T, _final(), TICKETS, settings=ON, api_key="x")
    assert out["status"] == "failed" and "HTTP 500" in out["reason"] and out["seconds"] >= 0
    assert note_auto.generate(FakeSb(), T, _final(), TICKETS.iloc[0:0], settings=ON)["status"] == "failed"


def test_avg_falls_back_to_model_without_key(monkeypatch):
    _claude_ok(monkeypatch)
    sb = FakeSb()
    out = note_auto.generate(sb, T, _final(), TICKETS, work=_final(), settings={**ON, "note_auto_ticket_source": "avg"},
                             api_key="")
    assert out["source"] == "model" and "両方の予想平均を作れず" in out["reason"]
    assert sb.saved()["p_ticket_source"] == "model"


def test_write_log_sends_note_fields_only_when_given(monkeypatch):
    sent = []
    monkeypatch.setattr(ap, "_sb", lambda method, path, **kw: sent.append(kw["json"]))
    ap.write_log(T, None, "done", "x")
    assert "note_status" not in sent[-1]
    ap.write_log(T, None, "done", "x", **ap.note_extra({"status": "ok", "reason": None, "seconds": 4.2}))
    assert sent[-1]["note_status"] == "ok" and sent[-1]["note_seconds"] == 4.2 and sent[-1]["note_at"]
    assert ap.note_extra(None) == {}


def test_make_note_off_returns_none():
    class Ctx:
        settings = {"note_auto_enabled": False}
        runtime = {}

    assert ap.make_note(Ctx(), T, _final(), TICKETS) is None
    assert ap.make_note_from_saved(Ctx(), T) is None
