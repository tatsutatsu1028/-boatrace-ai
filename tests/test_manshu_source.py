"""
万舟チャンスの予想3種比較（manshu_source.py）と、展示後の自動予想からの呼び出しの、
Supabase・公式サイト・Claude が無くても確かめられる部分。

  python tests/test_manshu_source.py   （pytest でも動く）
"""

from __future__ import annotations

import itertools
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manshu_source as ms  # noqa: E402

JST = ms.JST
T = {"race_key": "20261025_01_5", "race_date": "2026-10-25", "jcd": "01", "venue": "桐生", "race_no": 5,
     "deadline": "11:30", "sources": ["manshu"]}
COMBOS = ["-".join(map(str, c)) for c in itertools.permutations(range(1, 7), 3)]


def _tri():
    """1号艇が強い3連単の確率（合計1）。"""
    w = [(3.0 if c[0] == "1" else 1.0) / (1 + COMBOS.index(c) % 7) for c in COMBOS]
    s = sum(w)
    return pd.DataFrame({"combo": COMBOS, "prob": [x / s for x in w]})


def _fake_anthropic(monkeypatch, payload, stop_reason="end_turn"):
    """anthropic の代わり（受け取った引数を残し、payload を JSON で返す）。"""
    import json
    import types

    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)
            return types.SimpleNamespace(
                model=kw["model"], stop_reason=stop_reason,
                usage=types.SimpleNamespace(input_tokens=1000, output_tokens=2000),
                content=[types.SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()
            self.beta = types.SimpleNamespace(messages=Messages())

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    return seen


def _race_final():
    race = pd.DataFrame({"lane": range(1, 7), "racer_name": list("abcdef")})
    final = pd.DataFrame({"lane": range(1, 7), "p_first": [0.5, 0.15, 0.12, 0.1, 0.08, 0.05]})
    return race, final


def test_read_longshots_end_to_end(monkeypatch):
    import claude_reader as cr

    seen = _fake_anthropic(monkeypatch, {"summary": "s", "tickets": [
        {"combo": "1-2-3", "prob": 0.2, "reason": "x"}, {"combo": "4-1-2", "prob": 0.02, "reason": "y"}]})
    race, final = _race_final()
    r = cr.read_longshots("k", "rk-ls", race, final, model="claude-opus-5-5", race_label="2026-10-25 桐生 5R")
    assert r["status"] == "ok" and r["tickets"] == [{"combo": "4-1-2", "prob": 0.02, "reason": "y"}]
    assert r["called_api"] and r["cost"]["usd"] > 0 and r["prompt_version"] == cr.LONGSHOT_PROMPT_VERSION
    assert seen["system"] == cr.LONGSHOT_SYSTEM_PROMPT
    assert "桐生 5R" in seen["messages"][0]["content"]  # 入力は read_race と同じ build_brief
    assert cr.build_brief(race, final, "2026-10-25 桐生 5R") == __import__("json").loads(seen["messages"][0]["content"])


def test_read_race_still_works_after_refactor(monkeypatch):
    import claude_reader as cr

    _fake_anthropic(monkeypatch, {"summary": "s", "boats": [
        {"lane": i, "p_first": p, "reason": ""} for i, p in zip(range(1, 7), [3, 1, 1, 1, 1, 1])],
        "tickets": [{"combo": "1-2-3", "group": "本線", "prob": 0.1, "reason": ""}]})
    race, final = _race_final()
    r = cr.read_race("k", "rk-rr", race, final, model="claude-opus-5-5")
    assert r["status"] == "ok" and abs(r["p_first"][1] - 0.375) < 1e-9 and r["tickets"][0]["combo"] == "1-2-3"
    _fake_anthropic(monkeypatch, {}, stop_reason="max_tokens")
    r = cr.read_race("k", "rk-rr2", race, final, model="claude-opus-5-5")
    assert r["status"] == "error" and r["error"] == "回答が途中で切れました" and r["called_api"]


def _odds(v=50.0):
    return {c: (5.0 if c[0] == "1" else v) for c in COMBOS}


def test_compare_tickets_is_production_rule():
    import manshu

    probs = dict(zip(COMBOS, _tri()["prob"]))
    odds = {c: (5.0 if c[0] == "1" else (50.0 if int(c[2]) % 2 else 300.0)) for c in COMBOS}
    t = ms.compare_tickets(probs, odds)
    assert t == manshu.aim_tickets(probs, odds)
    assert [x["tier"] for x in t].count("中穴") == 5 and [x["tier"] for x in t].count("万舟") == 5
    assert all(x["combo"][0] != "1" for x in t)
    assert ms.method_name() == manshu.aim_method_name()


def test_source_probs_claude_not_filled_by_model():
    tri = _tri()
    reading = {"status": "ok", "tickets": [{"combo": "4-5-6", "prob": 0.03}, {"combo": "3-2-1", "prob": 0.02}]}
    p = ms.source_probs(tri, reading)
    assert set(p) == {"model", "claude", "blend"}
    assert p["claude"] == {"4-5-6": 0.03, "3-2-1": 0.02}  # 挙げた組だけ
    m = p["model"]
    assert len(p["blend"]) == 120
    assert abs(p["blend"]["4-5-6"] - (m["4-5-6"] + 0.03) / 2) < 1e-12
    assert abs(p["blend"]["2-1-3"] - m["2-1-3"] / 2) < 1e-12  # 挙げなかった組の Claude の確率は 0
    assert set(ms.source_probs(tri, {"status": "error"})) == {"model"}


def test_claude_only_tickets_stay_short():
    probs = {"4-5-6": 0.03, "3-2-1": 0.02, "2-3-4": 0.01, "5-1-2": 0.01}
    odds = {"4-5-6": 400.0, "3-2-1": 60.0, "2-3-4": 12.0, "5-1-2": 150.0}
    r = ms.build_row(T, "claude", probs, odds, None, claude_listed_count=4)
    assert [x["combo"] for x in r["aim_tickets"]] == ["3-2-1", "4-5-6", "5-1-2"]  # 30倍未満は外れる・埋めない
    assert r["aim_count"] == 3 and r["stake"] == 300 and r["claude_listed_count"] == 4 and r["error"] is None
    r = ms.build_row(T, "claude", {"2-3-4": 0.1}, odds, None, claude_listed_count=1)
    assert r["aim_count"] == 0 and "Claude が挙げた組" in r["error"]


def test_build_row_errors_and_deadline():
    probs = dict(zip(COMBOS, _tri()["prob"]))
    r = ms.build_row(T, "model", probs, _odds(), "2026-10-25T11:12:00+09:00")
    assert r["error"] is None and r["aim_count"] == 10 and r["stake"] == 1000 and r["claude_listed_count"] is None
    assert r["deadline_at"] == "2026-10-25T11:30:00+09:00"
    assert ms.build_row(T, "model", probs, {}, None)["error"] == "オッズを取れませんでした"
    assert "30倍以上" in ms.build_row(T, "model", probs, _odds(10.0), None)["error"]
    r = ms.build_row(T, "claude", None, _odds(), None, error="上限")
    assert r["error"] == "上限" and r["aim_tickets"] == [] and r["tri_probs"] is None and r["stake"] is None


def test_clean_longshots():
    import claude_reader as cr

    rows = [{"combo": "1-2-3", "prob": 0.2, "reason": ""}, {"combo": "3-1-5", "prob": 0.05, "reason": "a"},
            {"combo": "3-1-5", "prob": 0.05, "reason": "重複"}, {"combo": "4-4-1", "prob": 0.01, "reason": ""},
            {"combo": "6ー2ー1", "prob": 0.01, "reason": "b"}, {"combo": "2-3-7", "prob": 0.01, "reason": ""}]
    out = cr.clean_longshots(rows, [1, 2, 3, 4, 5, 6])
    assert out == [{"combo": "3-1-5", "prob": 0.05, "reason": "a"}, {"combo": "6-2-1", "prob": 0.01, "reason": "b"}]


class _SB:
    """Supabase の代わり。"""

    def __init__(self, spent=0.0, limit=3.0):
        self.rows = {}
        self.usage = []
        self.spent, self.limit = spent, limit
        self.patches = []

    def __call__(self, method, path, **kw):
        fake = self
        data = None
        if path.startswith(ms.TABLE) and method == "POST":
            data = []
            for r in kw["json"]:
                k = (r["race_key"], r["source"])
                if k not in fake.rows:
                    fake.rows[k] = dict(r)
                    data.append(r)
        elif path.startswith(ms.TABLE) and method == "GET":
            data = [dict(r) for r in fake.rows.values() if not r.get("result_at")]
        elif path.startswith(ms.TABLE) and method == "PATCH":
            src = path.split("source=eq.")[1].split("&")[0]
            fake.rows[(T["race_key"], src)].update(kw["json"])
            fake.patches.append(src)
        elif path.startswith("app_settings"):
            data = [{ms.LIMIT_SETTING: fake.limit}]
        elif path == "claude_usage" and method == "GET":
            data = [{"usd": fake.spent}]
        elif path == "claude_usage" and method == "POST":
            fake.usage.append(kw["json"])

        class R:
            def json(self_inner):
                return data

        return R()


def _install(monkeypatch, reading=None):
    import claude_reader as cr
    import prediction

    calls = []
    monkeypatch.setattr(prediction, "trifecta", lambda final: _tri())

    def fake_read(api_key, race_key, race, final, model=None, race_label=""):
        calls.append({"api_key": api_key, "label": race_label})
        return reading or {"status": "ok", "model": "claude-opus-5-5", "served_model": "claude-opus-5-5",
                           "called_api": True, "cost": {"usd": 0.12}, "usage": {"input_tokens": 10},
                           "tickets": [{"combo": "4-5-6", "prob": 0.03}, {"combo": "3-2-1", "prob": 0.02}]}

    monkeypatch.setattr(cr, "read_longshots", fake_read)
    monkeypatch.setattr(cr, "read_race", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read_race は使わない")))
    return calls


def _odds_df(v=50.0):
    o = _odds(v)
    return pd.DataFrame({"combo": list(o), "odds": list(o.values())})


def test_record_writes_three_rows_once_and_logs_usage(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB()
    assert ms.record(sb, T, None, "final", _odds_df(), api_key="k") == "3種を記録（Claude が挙げた組 2点・買い目 2点）"
    assert set(sb.rows) == {(T["race_key"], s) for s in ms.SOURCES}
    rows = {s: sb.rows[(T["race_key"], s)] for s in ms.SOURCES}
    assert all(r["error"] is None for r in rows.values())
    assert rows["model"]["aim_count"] == 10 and rows["blend"]["aim_count"] == 10 and rows["claude"]["aim_count"] == 2
    assert rows["claude"]["claude_listed_count"] == 2 and rows["blend"]["claude_listed_count"] == 2
    assert rows["model"]["claude_listed_count"] is None
    assert rows["claude"]["claude_model"] == "claude-opus-5-5" and rows["model"]["claude_model"] is None
    assert sb.usage and sb.usage[0]["purpose"] == "manshu_compare" and sb.usage[0]["usd"] == 0.12
    assert calls[-1]["label"] == "2026-10-25 桐生 5R"
    # 2回目は何もしない（Claude も呼ばない）
    n = len(calls)
    assert ms.record(sb, T, None, "final", _odds_df(), api_key="k") == "記録済み"
    assert len(calls) == n


def test_record_skips_claude_over_daily_limit(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB(spent=3.2, limit=3.0)
    st = ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    assert st.startswith("モデルだけ記録") and "上限" in st
    assert not calls and not sb.usage
    for s in ("claude", "blend"):
        r = sb.rows[(T["race_key"], s)]
        assert "上限" in r["error"] and r["aim_tickets"] == []
    assert sb.rows[(T["race_key"], "model")]["error"] is None


def test_record_claude_error_kept(monkeypatch):
    _install(monkeypatch, reading={"status": "error", "error": "TimeoutError", "called_api": True,
                                   "cost": {"usd": 0.01}, "model": "m"})
    sb = _SB()
    st = ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    assert "TimeoutError" in st
    assert sb.rows[(T["race_key"], "blend")]["error"] == "TimeoutError"
    assert sb.usage  # 失敗でも費用がかかった分は記録する


def test_result_fields_and_apply(monkeypatch):
    _install(monkeypatch)
    sb = _SB()
    ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    hit_combo = sb.rows[(T["race_key"], "model")]["aim_tickets"][0]["combo"]
    assert ms.apply_result(sb, T["race_key"], [(hit_combo, 5230.0)]) == 3
    r = sb.rows[(T["race_key"], "model")]
    assert r["result_status"] == "settled" and r["hit"] is True and r["stake"] == 1000 and r["return"] == 5230
    assert r["trifecta_actual"] == hit_combo and r["trifecta_payout"] == 5230
    assert ms.apply_result(sb, T["race_key"], [(hit_combo, 5230.0)]) == 0  # 2回目は書かない
    assert ms.result_fields({"aim_tickets": []}, [("返還", 100)])["result_status"] == "void"
    assert ms.result_fields({"aim_tickets": []}, [("1-2-3", 500.0)])["hit"] is None


def test_spent_today_queries_jst_midnight():
    seen = {}

    def sb(method, path, **kw):
        seen.update(path=path, params=kw.get("params"))

        class R:
            def json(self):
                return [{"usd": "0.5"}, {"usd": 0.25}]

        return R()

    now = datetime(2026, 10, 25, 1, 30, tzinfo=JST)
    assert ms.spent_today(sb, now) == 0.75
    assert ("called_at", "gte.2026-10-25T00:00:00+09:00") in seen["params"]
    assert ("purpose", "eq.manshu_compare") in seen["params"]


# ---------------------------------------------------------------
# 展示後の自動予想からの呼び出し
# ---------------------------------------------------------------
def test_auto_post_calls_compare_for_manshu_even_when_already(monkeypatch):
    import auto_post_predict as ap
    from test_auto_post_predict import _Fake, _pick

    fake = _Fake(exhibition=6, snapshot={"collector_name": "owner", "pre_exhibition": False})
    fake.install(monkeypatch)
    seen = []
    monkeypatch.setattr(ap, "compare_sources", lambda ctx, t, *a, **k: seen.append((t["race_key"], a)) or "3種を記録")
    pre_only, both = ap.merge_targets([_pick("01", 5, "11:30"), _pick("02", 1, "11:40")], [_pick("02", 1, "11:40")])
    assert ap.handle_window(None, pre_only, None) == "already"
    assert ap.handle_window(None, both, None) == "already"
    assert seen == [(both["race_key"], ())]  # 万舟チャンスだけ・予想し直す（final を渡さない）

    fake2 = _Fake(exhibition=6)
    fake2.install(monkeypatch)
    seen.clear()
    monkeypatch.setattr(ap, "compare_sources", lambda ctx, t, *a, **k: seen.append((t["race_key"], a)) or "3種を記録")
    assert ap.handle_window(None, both, None) == "done"
    assert len(seen) == 1 and seen[0][0] == both["race_key"] and len(seen[0][1]) == 3  # 保存した予想を渡す


def test_compare_sources_never_raises(monkeypatch):
    import auto_post_predict as ap
    import manshu_source

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(manshu_source, "recorded", boom)
    assert ap.compare_sources(None, dict(T)) == "エラー（RuntimeError）"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
