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

    def __init__(self, spent=0.0, limit=3.0, memo=None):
        self.rows = {}
        self.memo = memo
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
                if "merge-duplicates" in (kw.get("prefer") or ""):
                    fake.rows.setdefault(k, {}).update(r)
                elif k not in fake.rows:
                    fake.rows[k] = dict(r)
                    data.append(r)
        elif path.startswith(ms.TABLE) and method == "GET" and "source=eq." in path:
            src = path.split("source=eq.")[1].split("&")[0]
            data = [dict(r) for r in fake.rows.values() if r["source"] == src]
        elif path.startswith(ms.TABLE) and method == "GET":
            data = [dict(r) for r in fake.rows.values() if not r.get("result_at")]
        elif path.startswith(ms.MEMO_TABLE):
            data = [dict(fake.memo)] if fake.memo else []
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

    def fake_read(api_key, race_key, race, final, model=None, race_label="", memo=None):
        calls.append({"api_key": api_key, "label": race_label, "memo": memo})
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
    assert ms.record(sb, T, None, "final", _odds_df(), api_key="k") == (
        "3種を記録（Claude が挙げた組 2点・買い目 2点）・メモ入り: 記録なし")
    assert set(sb.rows) == {(T["race_key"], s) for s in ms.SOURCES}
    rows = {s: sb.rows[(T["race_key"], s)] for s in ms.SOURCES}
    assert all(rows[s]["error"] is None for s in ("model", "claude", "blend"))
    assert rows[ms.MEMO_SOURCE]["error"] == ms.NO_MEMO
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
    assert ms.apply_result(sb, T["race_key"], [(hit_combo, 5230.0)]) == 4
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
    assert ("purpose", "in.(manshu_compare,manshu_compare_memo)") in seen["params"]


# ---------------------------------------------------------------
# メモ入りの Claude（claude_memo）
# ---------------------------------------------------------------
MEMO = {"memo": "4号艇 伸び良し、1号艇 出足不安", "saved_at": "2026-10-25T02:10:00+00:00"}


def test_no_memo_is_recorded_as_none_not_filled_by_model(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB()
    ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    r = sb.rows[(T["race_key"], ms.MEMO_SOURCE)]
    assert r["error"] == ms.NO_MEMO and r["tri_probs"] is None and r["aim_tickets"] == [] and r["stake"] is None
    assert r["memo_saved_at"] is None
    assert len(calls) == 1 and calls[0]["memo"] is None  # メモが無ければ Claude は1回だけ
    assert "memo_saved_at" not in sb.rows[(T["race_key"], "model")]  # ほかの行は列を送らない


def test_record_with_memo_calls_claude_with_memo(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB(memo=MEMO)
    st = ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    assert st.endswith("メモ入り: 記録（Claude が挙げた組 2点・買い目 2点）")
    assert [c["memo"] for c in calls] == [None, MEMO["memo"]]
    r = sb.rows[(T["race_key"], ms.MEMO_SOURCE)]
    assert r["memo_saved_at"] == MEMO["saved_at"] and r["error"] is None and r["aim_count"] == 2
    assert r["tri_probs"] == {"4-5-6": 0.03, "3-2-1": 0.02} and r["claude_listed_count"] == 2
    assert [u["purpose"] for u in sb.usage] == ["manshu_compare", "manshu_compare_memo"]


def test_memo_claude_error_kept_with_memo_time(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB(spent=3.5, memo=MEMO)
    ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    r = sb.rows[(T["race_key"], ms.MEMO_SOURCE)]
    assert "上限" in r["error"] and r["memo_saved_at"] == MEMO["saved_at"] and r["aim_tickets"] == []
    assert not calls


def test_needs_memo_update():
    later = {"memo": "x", "saved_at": "2026-10-25T02:20:00+00:00"}
    assert ms.needs_memo_update(MEMO, None)
    assert ms.needs_memo_update(MEMO, {"memo_saved_at": None})  # 記録なし → メモが保存された
    assert not ms.needs_memo_update(MEMO, {"memo_saved_at": "2026-10-25T11:10:00+09:00"})  # 同じ時刻（表し方違い）
    assert ms.needs_memo_update(later, {"memo_saved_at": MEMO["saved_at"]})
    assert ms.needs_memo_update(None, {"memo_saved_at": MEMO["saved_at"]})  # メモを消した
    assert not ms.needs_memo_update(None, {"memo_saved_at": None})
    assert not ms.needs_memo_update(later, {"memo_saved_at": None, "result_at": "x"})  # 結果の出た行は触らない


def test_sync_memo_replaces_row(monkeypatch):
    calls = _install(monkeypatch)
    sb = _SB()
    ms.record(sb, T, None, "final", _odds_df(), api_key="k")
    assert ms.sync_memo(sb, T, None, "final", _odds_df(), api_key="k", memo=MEMO).startswith("作り直し: 記録")
    r = sb.rows[(T["race_key"], ms.MEMO_SOURCE)]
    assert r["memo_saved_at"] == MEMO["saved_at"] and r["aim_count"] == 2 and r["error"] is None
    assert r["saved_at"] and calls[-1]["memo"] == MEMO["memo"]
    # メモを消したら「記録なし」に戻す（古い確率・買い目を残さない）
    assert ms.sync_memo(sb, T, None, "final", _odds_df(), api_key="k", memo=None) == "作り直し: 記録なし"
    r = sb.rows[(T["race_key"], ms.MEMO_SOURCE)]
    assert r["memo_saved_at"] is None and r["tri_probs"] is None and r["aim_tickets"] == [] and r["claude_model"] is None


def test_read_longshots_with_memo(monkeypatch):
    import json

    import claude_reader as cr

    seen = _fake_anthropic(monkeypatch, {"summary": "s", "tickets": [{"combo": "4-1-2", "prob": 0.02, "reason": "y"}]})
    race, final = _race_final()
    r = cr.read_longshots("k", "rk-memo", race, final, model="claude-opus-5-5", race_label="L", memo=" 4号艇 伸び良し ")
    assert r["status"] == "ok" and r["prompt_version"] == cr.LONGSHOT_MEMO_PROMPT_VERSION
    assert seen["system"] == cr.LONGSHOT_MEMO_SYSTEM_PROMPT and seen["system"].startswith(cr.LONGSHOT_SYSTEM_PROMPT)
    brief = json.loads(seen["messages"][0]["content"])
    assert brief.pop(cr.MEMO_LABEL) == "4号艇 伸び良し"
    assert brief == cr.build_brief(race, final, "L")  # ほかの入力は Claude のみと同じ
    r2 = cr.read_longshots("k", "rk-memo", race, final, model="claude-opus-5-5", race_label="L")
    assert r2["input_hash"] != r["input_hash"] and r2["prompt_version"] == cr.LONGSHOT_PROMPT_VERSION


def test_pending_memos(monkeypatch):
    import auto_post_predict as ap

    t = dict(T)
    t2 = {**T, "race_key": "20261025_02_3", "deadline": "10:00"}  # 締切後
    t3 = {**T, "race_key": "20261025_03_4"}  # 自動記録の前
    t4 = {**T, "race_key": "20261025_04_6"}  # 作り直し済み
    compare = [{"race_key": k, "source": "model"} for k in (t["race_key"], t2["race_key"], t4["race_key"])]
    compare += [{"race_key": t["race_key"], "source": ms.MEMO_SOURCE, "memo_saved_at": None},
                {"race_key": t4["race_key"], "source": ms.MEMO_SOURCE, "memo_saved_at": MEMO["saved_at"]}]
    memos = [{**MEMO, "race_key": k} for k in (t["race_key"], t2["race_key"], t3["race_key"], t4["race_key"])]
    seen = []

    def sb(method, path, **kw):
        seen.append(path)
        data = compare if path.startswith(ms.TABLE) else memos

        class R:
            def json(self):
                return data

        return R()

    monkeypatch.setattr(ap, "_sb", sb)
    now = datetime(2026, 10, 25, 11, 20, tzinfo=JST)
    out = ap.pending_memos([t, t2, t3, t4, {**T, "race_key": "x", "sources": ["pre"]}], now)
    assert [(x["race_key"], m["memo"]) for x, m in out] == [(t["race_key"], MEMO["memo"])]
    assert ap.pending_memos([t, t2], now, race_key=t2["race_key"]) == []
    assert ap.pending_memos([t], datetime(2026, 10, 25, 11, 31, tzinfo=JST)) == []  # 締切後は作り直さない


def test_sync_memos_never_raises(monkeypatch):
    import auto_post_predict as ap

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ap, "pending_memos", boom)
    assert ap.sync_memos(None, [dict(T)]) is None


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
