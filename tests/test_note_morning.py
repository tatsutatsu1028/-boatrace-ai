"""
note の「朝の記事＋自動更新」（note_morning.py）と、展示後の自動予想（auto_post_predict.py）への組み込みの、
Supabase・Claude・公式サイトに繋がなくても確かめられる部分。

  python -m pytest tests/test_note_morning.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_post_predict as ap  # noqa: E402
import note_article as na  # noqa: E402
import note_auto  # noqa: E402
import note_morning as nm  # noqa: E402

ON = {"note_auto_enabled": True, "note_mode": "morning", "note_auto_scope": "pre_manshu"}
PICK = {"race_key": "20261109_04_5", "race_date": "2026-11-09", "jcd": "04", "venue": "平和島", "race_no": 5,
        "deadline": "11:30", "favorite_lane": 1, "favorite_name": "A", "p_first": [.5, .15, .12, .1, .08, .05],
        "tickets": [{"combo": "1-2-3", "group": "本線", "stake": 500}, {"combo": "1-2-4", "group": "本線", "stake": 300},
                    {"combo": "1-3-2", "group": "抑え", "stake": 200}]}
T = {"race_key": PICK["race_key"], "race_date": "2026-11-09", "jcd": "04", "venue": "平和島", "race_no": 5,
     "deadline": "11:30", "sources": ["pre"]}


class _R:
    def __init__(self, d):
        self.d = d

    def json(self):
        return self.d


class Sb:
    def __init__(self, save="created"):
        self.calls, self.save = [], save

    def __call__(self, method, path, **kw):
        self.calls.append((path, kw.get("json")))
        if path == "rpc/note_morning_save":
            return _R(self.save)
        if path == "rpc/note_morning_post_update":
            return _R(kw["json"]["p_status"])
        return _R([])

    def body(self, path):
        return next(j for p, j in self.calls if p == path)


def _claude(monkeypatch, seen=None):
    def fake(api_key, race_key, brief, tickets, model=None, force=False, system=None, schema=None,
             keys=("lead", "development", "ticket_comment"), compose_fn=None, prompt_version=None):
        if seen is not None:
            seen.update(system=system, version=prompt_version)
        parts = {"lead": "おはようございます！平和島5Rです。", "development": "スタートは内がそろいそうです。1号艇の逃げを本線に見ます。",
                 "ticket_comment": "1-2-3 を本線にしました。"}
        return {"status": "ok", "parts": parts, "body": compose_fn(parts) if compose_fn else "", "called_api": False}
    monkeypatch.setattr(na, "write_with_claude", fake)


def test_enabled_needs_mode_and_switch():
    assert nm.enabled(ON) and not nm.enabled({**ON, "note_mode": "post"}) and not nm.enabled({**ON, "note_auto_enabled": False})
    assert not nm.enabled({}) and nm.applies(ON, T) and not nm.applies(ON, {**T, "sources": ["manshu"]})


def test_morning_article(monkeypatch):
    seen = {}
    _claude(monkeypatch, seen)
    sb = Sb()
    res = nm.make_article(sb, PICK)
    assert res["status"] == "created" and res["writer"] == "claude"
    j = sb.body("rpc/note_morning_save")
    free, paid = note_auto.split_by_mark(j["p_article_text"])
    assert j["p_title"] == "【有料予想】平和島 5R｜締切 11:30" and j["p_price"] == 300
    assert nm.NOTICE in free and "展示後に買い目を更新します。更新は有料部分のリンク先でご確認ください" in free
    assert not na._has_combo(free) and "■ 展示前の予想展開\nスタートは内がそろいそうです。" in free
    assert "【本線買い目】\n1-2-34" in paid and "【抑え買い目】\n1-3-2" in paid
    assert paid.rstrip().endswith(f"{nm.LINK_HEAD}\n{{{{BUYER_URL}}}}")
    assert "締切10分前ごろ自動更新" in paid
    assert [t["combo"] for t in j["p_tickets"]] == ["1-2-3", "1-2-4", "1-3-2"]
    assert "展示前の予想展開" in j["p_pre_text"] and "【本線買い目】" in j["p_pre_text"]
    assert "展示航走の前（朝）" in seen["system"] and seen["version"] == nm.PROMPT_VERSION
    assert any(p == "rpc/set_note_header_image" for p, _ in sb.calls)


def test_morning_article_template_when_claude_fails():
    sb = Sb()
    res = nm.make_article(sb, PICK, api_key="")
    j = sb.body("rpc/note_morning_save")
    assert res["writer"] == "template" and nm.NOTICE in j["p_article_text"] and "{{BUYER_URL}}" in j["p_article_text"]


def test_targets_skip_soon_deadlines():
    rows = [{**PICK, "deadline": "09:05"}, {**PICK, "race_key": "x", "deadline": "09:30"}, {**PICK, "deadline": "bad"}]
    now = datetime(2026, 11, 9, 9, 2, tzinfo=nm.JST)
    assert [r["race_key"] for r in nm.targets(rows, now)] == ["x"]


def test_update_post_and_none(monkeypatch):
    _claude(monkeypatch)
    sb = Sb()
    final = pd.DataFrame({"lane": range(1, 7), "p_first": [.4, .2, .15, .1, .1, .05]})
    tk = [{"combo": "1-3-2", "group": "本線"}, {"combo": "1-3-4", "group": "抑え"}]
    out = nm.update_post(sb, T, final, tk, api_key="k")
    j = sb.body("rpc/note_morning_post_update")
    assert out["status"] == "updated" and j["p_status"] == "updated"
    assert j["p_post_tickets"] == tk and "■ 展示後の予想展開" in j["p_post_text"] and "【本線買い目】\n1-3-2" in j["p_post_text"]
    assert nm.mark_none(sb, T) == "none"


def _ctx(settings):
    class C:
        pass
    c = C()
    c.settings, c.runtime = settings, {}
    return c


def test_auto_post_updates_morning_instead_of_draft(monkeypatch):
    called = []
    monkeypatch.setattr(nm, "update_post_from_env", lambda sb, t, f, tk, w=None: called.append("morning") or {"status": "updated", "seconds": 1.0})
    monkeypatch.setattr(note_auto, "generate_from_env", lambda *a, **k: called.append("draft") or {"status": "ok"})
    final = pd.DataFrame({"lane": range(1, 7), "p_first": [.4, .2, .15, .1, .1, .05]})
    assert ap.make_note(_ctx(ON), T, final, []) is None and called == ["morning"]
    # 万舟チャンスにも入ったレース: 展示後の記事は作らず、万舟チャンスの通知には「manual」を返す
    called.clear()
    out = ap.make_note(_ctx(ON), {**T, "sources": ["pre", "manshu"]}, final, [])
    assert called == ["morning"] and out["status"] == "manual"
    # 万舟チャンスだけのレースは今まで通り（展示後の記事）
    called.clear()
    ap.make_note(_ctx(ON), {**T, "sources": ["manshu"]}, final, [])
    assert called == ["draft"]
    # 「展示後の記事（今まで通り）」なら厳選10も展示後の記事
    called.clear()
    ap.make_note(_ctx({**ON, "note_mode": "post"}), T, final, [])
    assert called == ["draft"]


def test_late_race_marks_none(monkeypatch):
    marked = []
    monkeypatch.setattr(nm, "mark_none", lambda sb, t: marked.append(t["race_key"]))
    monkeypatch.setattr(ap, "snapshot_meta", lambda rk: None)
    monkeypatch.setattr(ap, "write_log", lambda t, prev, status, reason=None, **k: {"status": status})
    assert ap.handle_late(T, {"attempts": 2, "reason": "展示タイムがそろっていません（3/6）"}, _ctx(ON)) == "skipped"
    assert marked == [T["race_key"]]
    marked.clear()
    ap.handle_late(T, None, _ctx({**ON, "note_mode": "post"}))
    assert marked == []


def test_split_unchanged_for_post_articles():
    body = na.compose({"lead": "こんにちは", "development": "スタートは横並び。1号艇が逃げます。", "ticket_comment": "1-2-3"},
                      na.tickets_list(PICK["tickets"]))
    free, paid = na.split_article(body, "平和島 5R", "11:30")
    assert free.endswith(na.PAYWALL_LEAD) and "■ 展示後の予想展開\nスタートは横並び。" in free
