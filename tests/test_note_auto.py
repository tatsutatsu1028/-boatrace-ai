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


def test_note_made_when_auto_fix_saved_first(monkeypatch):
    """予想の直前に自動固定が保存していた（save_snapshot が auto_fixed）ときも、保存された予想で記事を作る。"""
    written, made = [], []
    race = pd.DataFrame({"exhibition_time": [6.8] * 6})
    monkeypatch.setattr(ap, "snapshot_meta", lambda rk: None)
    monkeypatch.setattr(ap, "predict_race", lambda ctx, t, race, odds: ({"final": _final(), "tickets": TICKETS}, "{}", "h"))
    monkeypatch.setattr(ap, "save_snapshot", lambda t, pj, ph: "auto_fixed")
    monkeypatch.setattr(ap, "make_note_from_saved", lambda ctx, t: made.append(t["race_key"]) or {"status": "ok"})
    monkeypatch.setattr(ap, "write_log", lambda t, prev, status, reason=None, **k: written.append((status, k)) or
                        {"status": status})
    import official_fetcher
    monkeypatch.setattr(official_fetcher, "fetch_official_race", lambda *a: race)
    monkeypatch.setattr(official_fetcher, "fetch_odds3t", lambda *a: None)
    assert ap.handle_window(object(), T, None) == "already"
    assert made == [T["race_key"]] and written[-1][1]["note"] == {"status": "ok"}


def test_header_recorded_for_auto_draft(monkeypatch):
    _claude_ok(monkeypatch)
    sb = FakeSb()
    out = note_auto.generate(sb, {**T, "sources": ["pre"]}, _final(), TICKETS, settings=ON, api_key="x")
    row = next(kw["json"] for m, p, kw in sb.calls if p == "rpc/set_note_header_image")
    assert out["header"] == "gensen" and row["p_image"] == row["p_auto"] == "gensen"
    # 手動の記事があるレースは見出し画像も書かない
    sb2 = FakeSb(save_result="manual")
    note_auto.generate(sb2, T, _final(), TICKETS, settings=ON, api_key="x")
    assert not any(p == "rpc/set_note_header_image" for m, p, kw in sb2.calls)


def test_choose_header_rules():
    rough = pd.DataFrame({"wind_speed": [7.0] * 6, "wave_height": [3.0] * 6})
    assert note_auto.choose_header({"sources": ["pre"]}, rough)[0] == "aresuimen"
    assert note_auto.choose_header({"sources": ["pre"]})[0] == "gensen"
    assert note_auto.choose_header({"sources": []})[0] == "shobu"


def test_report_builds_note_variants(monkeypatch):
    """1日の締めの結果報告記事: 確定済みの記事を記事の買い目で判定し、3パターンの文面を作る。"""
    import official_fetcher
    import threads_poster as tp
    import note_report

    recs = [{"section": "note", "race_key": "20261107_04_5", "venue": "平和島", "race_no": 5, "sequence_no": 1,
             "publication_type": "PAID", "ticket_source": "model", "tickets": ["1-2-3", "1-2-4"], "legacy": {},
             "trifecta_actual": ""},
            {"section": "note", "race_key": "20261107_04_9", "venue": "平和島", "race_no": 9, "sequence_no": 2,
             "publication_type": "PAID", "ticket_source": "model", "tickets": ["2-1-3"], "legacy": {},
             "trifecta_actual": ""}]
    monkeypatch.setattr(tp, "fetch_daily_publication_results", lambda u, k, d: [tp.judge_record(dict(r)) for r in recs])
    monkeypatch.setattr(official_fetcher, "fetch_venue_result_list", lambda d8, code: pd.DataFrame(
        [{"race_no": 5, "trifecta": "1-2-3", "payout_per_100": 1230}]))
    rep = note_report.build("u", "k", "2026-11-07")
    assert rep["settled"] == 1 and [r["race_no"] for r in rep["pending"]] == [9]
    assert len(rep["variants"]) == 3 and all(v["title"] and v["body"] for v in rep["variants"])
    assert "平和島" in rep["variants"][0]["body"]


def test_scope_pre_only_by_default(monkeypatch):
    """対象は既定で「厳選のみ」: 万舟チャンスだけのレースは記事を作らない。厳選10にも入ったレースは作る。"""
    _claude_ok(monkeypatch)
    manshu_only, both = {**T, "sources": ["manshu"]}, {**T, "sources": ["pre", "manshu"]}
    assert note_auto.scope(ON) == "pre" and note_auto.scope({"note_auto_scope": "x"}) == "pre"
    sb = FakeSb()
    assert note_auto.generate(sb, manshu_only, _final(), TICKETS, settings=ON, api_key="x")["status"] == "off"
    assert sb.calls == []
    assert note_auto.applies(ON, both) and note_auto.applies(ON, T)
    wide = {**ON, "note_auto_scope": "pre_manshu"}
    assert note_auto.applies(wide, manshu_only)
    assert note_auto.generate(FakeSb(), manshu_only, _final(), TICKETS, settings=wide, api_key="x")["status"] == "ok"
    assert not note_auto.applies({"note_auto_enabled": False, "note_auto_scope": "pre_manshu"}, manshu_only)


def test_make_note_skips_manshu_only_race():
    class Ctx:
        settings = ON
        runtime = {}

    assert ap.make_note(Ctx(), {**T, "sources": ["manshu"]}, _final(), TICKETS) is None
    assert ap.make_note_from_saved(Ctx(), {**T, "sources": ["manshu"]}) is None


# ---------------------------------------------------------------
# 万舟チャンスのレース: 2種類の買い目
# ---------------------------------------------------------------
WIDE = {**ON, "note_auto_scope": "pre_manshu", "note_auto_price_manshu": 500}
TM = {**T, "race_key": "20261107_14_7", "venue": "鳴門", "race_no": 7, "deadline": "13:36", "sources": ["manshu"]}
AIM = [{"combo": "2-1-4", "tier": "中穴", "odds": 59.5}, {"combo": "1-3-2", "tier": "中穴", "odds": 31.0},
       {"combo": "2-1-5", "tier": "万舟", "odds": 178.7}]
MARK = {"aim_tickets": AIM, "aim_saved_at": "2026-11-07 04:26:30+00", "post_saved_at": "2026-11-07T13:26:26+09:00",
        "post_head_axis_lane": 2, "head_axis_lane": 5, "manshu_prob": 0.25, "post_manshu_prob": 0.29,
        "post_flag_reasons": ["本命 1→5号艇"]}


class MarkSb(FakeSb):
    def __init__(self, mark, **kw):
        super().__init__(**kw)
        self.mark = mark

    def __call__(self, method, path, **kw):
        if path.startswith("manshu_chance_picks"):
            self.calls.append((method, path, kw))
            return _Resp([self.mark] if self.mark else [])
        return super().__call__(method, path, **kw)


def _claude_manshu(monkeypatch, seen):
    def fake(api_key, race_key, brief, tickets, model=na.ARTICLE_MODEL, force=False, system=None, schema=None,
             keys=("lead", "development", "ticket_comment"), compose_fn=None, prompt_version=None):
        seen.update(brief=brief, keys=keys, tickets=tickets)
        parts = {"lead": "こんにちは！万舟チャンスの鳴門7Rです。", "development": "スタートは5号艇が速そうです。2号艇の差しに期待します。",
                 "aim_comment": "本命の1着確率が低めで荒れる余地があります。頭本命の2号艇から 2-1-4 を狙います。",
                 "ticket_comment": "1-3-2 を本線にしました。"}
        problem = na.check_text(" ".join(parts[k] for k in keys), tickets)
        assert problem is None, problem
        return {"status": "ok", "body": compose_fn(parts), "called_api": False}
    monkeypatch.setattr(na, "write_with_claude", fake)


def test_manshu_article_has_both_tickets(monkeypatch):
    seen = {}
    _claude_manshu(monkeypatch, seen)
    sb = MarkSb(MARK)
    out = note_auto.generate(sb, TM, _final(), TICKETS, settings=WIDE, api_key="x")
    assert out["status"] == "ok" and out["manshu_aim"] is True
    row = sb.saved()
    text = row["p_article_text"]
    free, paid = note_auto.split_by_mark(text)
    assert row["p_title"] == "【万舟チャンス】【有料予想】鳴門 7R｜締切 13:36" and row["p_price"] == 500
    # 無料部分: 万舟チャンスのレースと分かる一言・組番なし
    assert note_auto.MANSHU_INTRO in free and "万舟チャンスの買い目" in free and not na._has_combo(free)
    # 有料部分: 2種類の買い目が見出しつきで両方入り、重なる組（1-3-2）には ※重複
    assert note_auto.MANSHU_AIM_HEAD in paid and note_auto.MANSHU_MODEL_HEAD in paid
    assert paid.index(note_auto.MANSHU_AIM_HEAD) < paid.index(note_auto.MANSHU_MODEL_HEAD)
    assert "【中穴（オッズ30〜99倍）】" in paid and "【万舟（オッズ100倍以上）】" in paid
    assert "【本線買い目】" in paid and "【抑え買い目】" in paid
    assert paid.count("1-3-2 ※重複") == 2 and note_auto.OVERLAP_NOTE in paid
    assert "2-1-4" in paid and "2-1-5" in paid and "1-2-34" in paid
    assert "頭本命の2号艇" in paid
    # 夜のまとめの的中判定には両方の買い目を使う
    assert [t["combo"] for t in row["p_article_tickets"]] == ["2-1-4", "1-3-2", "2-1-5", "1-2-3", "1-2-4", "1-3-2"]
    # Claude に渡すデータ: 2種類の買い目と万舟チャンスの情報（頭本命は展示後）
    assert seen["keys"] == note_auto.MANSHU_KEYS
    assert seen["brief"]["万舟チャンス"]["頭本命（1着軸）"] == "2号艇"
    assert seen["brief"]["万舟チャンス"]["展示後の注意"] == "本命 1→5号艇"
    assert set(seen["brief"]["載せる買い目"]) == {"万舟チャンスの買い目", "通常の予想"}


def test_manshu_without_rebuilt_aim_omits_aim(monkeypatch):
    """万舟チャンスの買い目を展示後に作り直せなかった（朝の買い目のまま）レースは、その欄を省いて一言書く。"""
    seen = {}
    _claude_manshu(monkeypatch, seen)
    morning = {**MARK, "aim_saved_at": "2026-11-06 23:15:39+00", "aim_source": "morning"}
    sb = MarkSb(morning)
    out = note_auto.generate(sb, TM, _final(), TICKETS, settings=WIDE, api_key="x")
    paid = note_auto.split_by_mark(sb.saved()["p_article_text"])[1]
    assert out["manshu_aim"] is False and "作り直せなかった" in out["reason"]
    assert "展示後に万舟チャンスの買い目を作り直せなかったため" in paid and "【中穴" not in paid
    assert note_auto.MANSHU_MODEL_HEAD in paid and "【本線買い目】" in paid
    assert sb.saved()["p_title"].startswith("【万舟チャンス】")
    assert seen["keys"] == ("lead", "development", "ticket_comment") and "万舟チャンス" not in seen["brief"]
    assert [t["combo"] for t in sb.saved()["p_article_tickets"]] == ["1-2-3", "1-2-4", "1-3-2"]


def test_manshu_template_when_claude_fails():
    sb = MarkSb(MARK)
    out = note_auto.generate(sb, TM, _final(), TICKETS, settings=WIDE, api_key="")
    paid = note_auto.split_by_mark(sb.saved()["p_article_text"])[1]
    assert out["status"] == "template"
    assert note_auto.MANSHU_AIM_HEAD in paid and note_auto.MANSHU_MODEL_HEAD in paid and "1-3-2 ※重複" in paid


def test_pre_race_article_unchanged(monkeypatch):
    """厳選10のレースの記事は今までと同じ（万舟チャンスの行を読まない・タイトル・価格・本文の形）。"""
    _claude_ok(monkeypatch)
    sb = MarkSb(MARK)
    note_auto.generate(sb, T, _final(), TICKETS, settings=WIDE, api_key="x")
    row = sb.saved()
    assert not any(p.startswith("manshu_chance_picks") for m, p, kw in sb.calls)
    assert row["p_title"] == "【有料予想】平和島 5R｜締切 11:30" and row["p_price"] == 300
    parts = {"lead": "こんにちは！平和島5Rの見どころです。",
             "development": "スタートは内がそろいそうです。1号艇が先に回る展開を本線に見ます。",
             "ticket_comment": "1-2-3 を本線にしました。"}
    expected = note_auto.with_paywall_mark(na.compose(parts, na.tickets_list(TICKETS)), "平和島 5R", "11:30")
    assert row["p_article_text"] == expected and "万舟" not in row["p_article_text"]
