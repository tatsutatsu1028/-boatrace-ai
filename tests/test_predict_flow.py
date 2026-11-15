"""predict_flow.save: 予想の保存のルール（アプリで計算するときも Actions で計算するときも同じ）。"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

import predict_flow as pf
import result_tracker

JST = pf.JST
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=JST)
TODAY = NOW.date()


@pytest.fixture
def calls(monkeypatch):
    log = {"save": [], "watch": [], "odds": []}

    def fake_save(**kw):
        log["save"].append(kw)
        return {"status": "inserted", "snapshot_kind": "same_day", "payload_hash": kw["payload_hash"]}

    monkeypatch.setattr(result_tracker, "save_latest_prediction_snapshot", fake_save)
    monkeypatch.setattr(result_tracker, "odds_tracking_available", lambda: True)
    monkeypatch.setattr(result_tracker, "add_to_odds_watchlist", lambda *a: (log["watch"].append(a) or (True, "ok")))
    monkeypatch.setattr(result_tracker, "save_odds_snapshot_now", lambda *a: (log["odds"].append(a) or (True, "保存")))
    monkeypatch.setattr(pf, "build_payload", lambda result: ("{}", "h1"))
    return log


def _result(pre=False):
    return {"final": pd.DataFrame({"lane": [1]}), "tickets": pd.DataFrame(), "pre_exhibition": pre}


def _save(meta, d=TODAY, deadline="12:30", **kw):
    return pf.save("20261115_18_12", d, "18", "徳山", 12, {12: deadline}, None, _result(**kw), "owner", meta, now=NOW)


def test_before_deadline_saves_and_starts_odds_tracking(calls):
    out = _save({"exists": False})
    assert out["status"] == "inserted" and out["rpc"] is True
    assert calls["save"][0]["collector_name"] == "owner"
    assert calls["watch"] == [("20261115", "18", 12)]
    assert "オッズ自動追跡" in out["odds_message"]


def test_after_deadline_is_not_overwritten(calls):
    assert _save({"exists": True}, deadline="11:59")["status"] == "closed"
    assert _save({"exists": False}, deadline="11:59")["status"] == "closed"
    assert calls["save"] == []


def test_past_day_saves_only_when_not_saved_yet(calls):
    assert _save({"exists": True}, d=date(2026, 11, 14))["status"] == "closed"
    assert _save({"exists": False}, d=date(2026, 11, 14))["status"] == "inserted"


def test_auto_random_is_never_overwritten(calls):
    assert _save({"exists": True, "snapshot_kind": "auto_random"})["status"] == "auto_fixed"
    assert calls["save"] == []


def test_same_content_is_not_written(calls):
    assert _save({"exists": True, "payload_hash": "h1", "pre_exhibition": False})["status"] == "unchanged"
    # 展示前の印が違えば書く
    assert _save({"exists": True, "payload_hash": "h1", "pre_exhibition": True})["status"] == "inserted"


def test_meta_unreadable_is_error(calls):
    assert _save(None)["status"] == "error"


def test_deadline_passed_rules():
    assert pf.deadline_passed(date(2026, 11, 14), {}, 1, NOW)
    assert not pf.deadline_passed(date(2026, 11, 16), {}, 1, NOW)
    assert not pf.deadline_passed(TODAY, {}, 1, NOW)           # 締切が分からなければ締切前扱い
    assert pf.deadline_passed(TODAY, {1: "12:00"}, 1, NOW)
    assert not pf.deadline_passed(TODAY, {1: "12:01"}, 1, NOW)
