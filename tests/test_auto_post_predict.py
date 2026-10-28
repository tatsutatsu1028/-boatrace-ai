"""
展示後の自動予想（auto_post_predict.py）の、Supabase・公式サイト・データ用リポジトリが無くても
確かめられる部分。

  python tests/test_auto_post_predict.py   （pytest でも動く）
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_post_predict as ap  # noqa: E402

JST = ap.JST
DATE8 = "20261022"


def _pick(jcd, rno, deadline, venue="会場"):
    return {"race_key": f"{DATE8}_{jcd}_{rno}", "race_date": "2026-10-22", "jcd": jcd, "venue": venue,
            "race_no": rno, "deadline": deadline}


def _at(hhmm):
    return datetime.strptime(f"2026-10-22 {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)


def test_merge_targets_dedupes_and_sorts():
    pre = [_pick("01", 5, "11:30"), _pick("04", 2, "10:05")]
    man = [_pick("01", 5, "11:30"), _pick("12", 9, "19:40")]
    t = ap.merge_targets(pre, man)
    assert [x["race_key"] for x in t] == [f"{DATE8}_04_2", f"{DATE8}_01_5", f"{DATE8}_12_9"]
    both = t[1]
    assert both["sources"] == ["pre", "manshu"]
    assert t[0]["sources"] == ["pre"] and t[2]["sources"] == ["manshu"]


def test_schedule_deadline_wins_and_cancelled_marked():
    t = ap.merge_targets([_pick("01", 5, "11:30"), _pick("02", 3, "12:00")], [])
    sched = [{"jcd": "01", "deadlines": {"5": "11:34"}, "status": "5R 11:34"},
             {"jcd": "02", "deadlines": {"3": "12:00"}, "status": "中止"}]
    ap.apply_schedule(t, sched)
    assert t[0]["deadline"] == "11:34" and not t[0].get("cancelled")
    assert t[1]["cancelled"] is True


def test_phase_boundaries():
    assert ap.phase(None) == "unknown"
    assert ap.phase(20.5) == "early"
    assert ap.phase(20) == "window"
    assert ap.phase(15) == "window"
    assert ap.phase(10) == "window"
    assert ap.phase(5) == "window"
    assert ap.phase(4.9) == "late"
    assert ap.phase(-3) == "late"


def test_plan_every_race_gets_three_or_four_checks():
    """5分おきの起動なら、締切20〜5分前のあいだに必ず3回（境目ちょうどなら4回）確かめ、
    そのうち1回は締切10〜5分前（展示タイムが遅く出る江戸川・常滑などでも間に合う）。"""
    picks = [_pick(f"{(i % 24) + 1:02d}", (i % 12) + 1, f"{8 + i // 4:02d}:{(i * 7) % 60:02d}") for i in range(52)]
    targets = ap.merge_targets(picks, [])
    for delay in (0, 2, 3, 4):
        for tg, runs in ap.plan(targets, DATE8, delay=delay):
            left = [ap.minutes_left(tg["race_date"], tg["deadline"], x) for x in runs]
            if tg["deadline"] < "08:20":
                continue  # 起動の時間帯（8:00〜）より前に締切20分前が来るレース
            assert 3 <= len(runs) <= 4, (tg["deadline"], delay, runs)
            assert all(5 <= m <= 20 for m in left)
            assert any(m <= 10 for m in left)


def test_plan_text_hides_venue_in_actions():
    targets = ap.merge_targets([_pick("01", 5, "11:30", venue="桐生")], [_pick("01", 5, "11:30", venue="桐生")])
    planned = ap.plan(targets, DATE8, delay=3)
    detail = ap.format_plan(planned, detail=True)[0]
    assert "桐生 5R" in detail and "11:13 → 11:18 → 11:23（3回まで）" in detail and "展示前厳選10・万舟チャンス" in detail
    assert "桐生" not in ap.format_plan(planned, detail=False)[0]


def test_already_reason():
    assert ap.already_reason(None) is None
    assert ap.already_reason({"collector_name": "owner", "pre_exhibition": True}) is None
    assert ap.already_reason({"collector_name": "owner", "pre_exhibition": False})[0] == "already"
    assert ap.already_reason({"collector_name": "auto_random", "pre_exhibition": False})[0] == "already"
    assert ap.already_reason({"collector_name": "auto_post", "pre_exhibition": False})[0] == "done"


class _Fake:
    """Supabase と公式サイトの代わり。"""

    def __init__(self, exhibition=6, snapshot=None):
        self.logs = {}
        self.exhibition = exhibition
        self.snapshot = snapshot
        self.saved = []
        self.aim = []

    def install(self, monkeypatch):
        import official_fetcher

        monkeypatch.setattr(ap, "snapshot_meta", lambda rk: self.snapshot)
        monkeypatch.setattr(ap, "_sb", self._sb)
        race = pd.DataFrame({"lane": range(1, 7),
                             "exhibition_time": [6.7] * self.exhibition + [None] * (6 - self.exhibition)})
        monkeypatch.setattr(official_fetcher, "fetch_official_race", lambda *a: race)
        monkeypatch.setattr(official_fetcher, "fetch_odds3t", lambda *a: pd.DataFrame({"combo": [], "odds": []}))
        monkeypatch.setattr(ap, "predict_race", lambda ctx, t, race, odds: (
            {"final": None, "model_info": {"model_used": "ml-chain-test"}}, "{}", "h"))
        monkeypatch.setattr(ap, "start_odds_tracking", lambda t, odds: None)
        monkeypatch.setattr(ap, "rebuild_aim", lambda t, final, odds: self.aim.append(t["race_key"]) or "作り直して保存（10点）")

    def _sb(self, method, path, **kw):
        fake = self

        class R:
            def json(self_inner):
                if path.startswith("rpc/save_latest_prediction_snapshot"):
                    fake.saved.append(kw["json"])
                    return {"status": "inserted"}
                return []

        if path.startswith(ap.LOG_TABLE) and method == "POST":
            self.logs[kw["json"]["race_key"]] = kw["json"]
        return R()


def test_window_not_ready_then_late_is_skipped_with_reason(monkeypatch):
    fake = _Fake(exhibition=4)
    fake.install(monkeypatch)
    t = ap.merge_targets([_pick("01", 5, "11:30")], [])[0]
    assert ap.handle_window(None, t, None) == "waiting"
    row = fake.logs[t["race_key"]]
    assert row["attempts"] == 1 and "4/6" in row["reason"]
    assert ap.handle_window(None, t, row) == "waiting"
    row = fake.logs[t["race_key"]]
    assert row["attempts"] == 2
    assert ap.handle_late(t, row) == "skipped"
    assert "見送り" in fake.logs[t["race_key"]]["reason"] and "4/6" in fake.logs[t["race_key"]]["reason"]
    assert not fake.saved


def test_window_ready_saves_as_auto_post_and_rebuilds_aim_for_manshu(monkeypatch):
    fake = _Fake(exhibition=6)
    fake.install(monkeypatch)
    pre_only, both = ap.merge_targets([_pick("01", 5, "11:30"), _pick("02", 1, "11:40")], [_pick("02", 1, "11:40")])
    assert ap.handle_window(None, pre_only, None) == "done"
    assert ap.handle_window(None, both, None) == "done"
    assert [s["p_collector_name"] for s in fake.saved] == ["auto_post", "auto_post"]
    assert all(s["p_pre_exhibition"] is False for s in fake.saved)
    assert fake.aim == [both["race_key"]]  # 万舟チャンスのレースだけ作り直す
    assert fake.logs[both["race_key"]]["aim_status"].startswith("作り直して保存")


def test_window_skips_when_already_predicted(monkeypatch):
    fake = _Fake(exhibition=6, snapshot={"collector_name": "owner", "pre_exhibition": False})
    fake.install(monkeypatch)
    t = ap.merge_targets([_pick("01", 5, "11:30")], [])[0]
    assert ap.handle_window(None, t, None) == "already"
    assert not fake.saved


def test_run_only_touches_window_and_late(monkeypatch):
    fake = _Fake(exhibition=6)
    fake.install(monkeypatch)
    targets = ap.merge_targets([_pick("01", 1, "10:00"), _pick("01", 5, "11:30"), _pick("01", 9, "14:00")], [])
    monkeypatch.setattr(ap, "load_targets", lambda d: targets)
    monkeypatch.setattr(ap, "load_log", lambda d: {})
    monkeypatch.setattr(ap, "Context", lambda: None)
    counts = ap.run(DATE8, now=_at("11:15"))
    assert counts == {"skipped": 1, "done": 1}
    assert fake.logs[f"{DATE8}_01_1"]["reason"] == ap.NEVER_RAN
    assert f"{DATE8}_01_9" not in fake.logs


def test_summarize_counts_and_reasons():
    targets = ap.merge_targets([_pick("01", 1, "10:00"), _pick("01", 2, "10:30"), _pick("01", 3, "11:00")],
                               [_pick("01", 3, "11:00"), _pick("01", 4, "21:00")])
    logged = {
        f"{DATE8}_01_1": {"status": "done", "aim_status": None},
        f"{DATE8}_01_2": {"status": "waiting", "reason": "展示タイムがそろっていません（5/6）", "attempts": 2},
        f"{DATE8}_01_3": {"status": "done", "aim_status": "作り直して保存（10点）"},
    }
    s = ap.summarize(targets, logged, _at("20:00"))
    assert s["targets"] == 4 and s["pre"] == 3 and s["manshu"] == 2 and s["both"] == 1
    assert s["status"] == {"done": 2, "skipped": 1, "waiting": 1}
    assert s["reasons"] == {"見送り: 展示タイムがそろっていません（5/6）": 1}
    assert s["aim"] == {"作り直して保存": 1}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
