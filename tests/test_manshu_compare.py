"""
万舟チャンスの本命の表示と、確定オッズで作る朝版・展示後版の比較（manshu.py・manshu_view.py・manshu_compare.py・
hindcast.py）の、データ用リポジトリや Supabase が無くても確かめられる部分。

  python tests/test_manshu_compare.py   （pytest でも動く）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manshu  # noqa: E402
import manshu_compare  # noqa: E402
import manshu_view  # noqa: E402
import pre_picks  # noqa: E402


def _tri(first):
    """1着確率 {艇番: 確率} → 3連単確率（1着ごとに2つの組に半分ずつ）。"""
    out = {}
    for lane, p in first.items():
        others = [x for x in range(1, 7) if x != lane]
        out[f"{lane}-{others[0]}-{others[1]}"] = p / 2
        out[f"{lane}-{others[1]}-{others[0]}"] = p / 2
    return out


def test_favorite_morning_and_post():
    r = {"favorite_lane": 1, "favorite_prob": 0.4321}
    assert manshu.favorite_now(r) == (1, 0.4321, "朝")
    assert manshu_view.favorite_text(r) == "本命 1号艇 43%（朝の予想）"
    r["post_tri_probs"] = _tri({1: 0.30, 3: 0.38, 2: 0.32})
    lane, p, which = manshu.favorite_now(r)
    assert (lane, round(p, 2), which) == (3, 0.38, "展示後")
    assert manshu_view.favorite_text(r) == "本命 3号艇 38%（展示後の予想）　朝は 1号艇"
    r["post_tri_probs"] = _tri({1: 0.5, 2: 0.5})
    assert manshu_view.favorite_text(r) == "本命 1号艇 50%（展示後の予想）"  # 同じ確率なら内側
    # 朝の1着確率が無い古い行は p_first から
    assert manshu.favorite_now({"favorite_lane": 2, "p_first": [0.1, 0.35, 0, 0, 0, 0]})[1] == 0.35
    assert manshu_view.favorite_text({}) is None


def _odds():
    # 1号艇1着以外は全部 50倍（中穴）か 150倍（万舟）
    out = {}
    for a in range(1, 7):
        for b in range(1, 7):
            for c in range(1, 7):
                if len({a, b, c}) == 3:
                    out[f"{a}-{b}-{c}"] = 5.0 if a == 1 else (50.0 if (a + b + c) % 2 else 150.0)
    return out


def test_compare_row_uses_same_odds_for_both():
    odds = _odds()
    pre = {k: (0.5 if k.startswith("2") else 0.1) / 20 for k in odds}
    post = {k: (0.5 if k.startswith("4") else 0.1) / 20 for k in odds}
    pick = {"race_key": "20261020_01_05", "race_date": "2026-10-20", "tri_probs": pre}
    row = manshu_compare.compare_row(pick, odds, ("ml-chain-v2-x+p", post), [("4-1-2", 15000)])
    assert [t["combo"][0] for t in row["pre_tickets"]] == ["2"] * 10
    assert [t["combo"][0] for t in row["post_tickets"]] == ["4"] * 10
    assert all(t["combo"][0] != "1" for t in row["pre_tickets"] + row["post_tickets"])
    assert row["pre_hit"] is False and row["pre_stake"] == 1000 and row["pre_return"] == 0
    assert row["post_hit"] is True and row["post_return"] == 15000 and row["is_manshu"] is True
    assert row["hindcast_model_version"] == "ml-chain-v2-x+p" and row["method"] == manshu.aim_method_name()
    # 確率が無い側は理由だけ
    row = manshu_compare.compare_row({**pick, "tri_probs": None}, odds, None, [("4-1-2", 15000)])
    assert row["pre_tickets"] is None and row["pre_error"] == "朝の3連単の確率がありません"
    assert row["post_error"] == "事後予想の3連単の確率がまだありません"
    assert not manshu_compare.done({"final_odds": odds, "post_error": row["post_error"]})
    assert manshu_compare.done({"final_odds": odds, "post_tickets": [{"combo": "4-1-2"}]})
    assert not manshu_compare.done({"final_odds": None, "post_tickets": [{"combo": "4-1-2"}]})


def test_compare_summary_counts_only_races_with_both():
    rows = [
        {"race_date": "2026-10-20", "is_manshu": True, "pre_hit": False, "pre_stake": 1000, "pre_return": 0,
         "post_hit": True, "post_stake": 1000, "post_return": 15000},
        {"race_date": "2026-10-20", "is_manshu": False, "pre_hit": True, "pre_stake": 1000, "pre_return": 4000,
         "post_hit": False, "post_stake": 1000, "post_return": 0},
        # 展示後版がまだ無いレースは数えない
        {"race_date": "2026-10-21", "is_manshu": True, "pre_hit": True, "pre_stake": 1000, "pre_return": 30000,
         "post_hit": None, "post_stake": None, "post_return": None},
    ]
    s = manshu.compare_summary(rows)
    assert s["レース数"] == 2
    assert s["朝版 的中率"] == 0.5 and s["朝版 回収率"] == 2.0 and s["朝版 万舟の的中"] == 0
    assert s["展示後版 的中率"] == 0.5 and s["展示後版 回収率"] == 7.5 and s["展示後版 万舟の的中"] == 1
    assert manshu.compare_summary(rows[2:]) is None and manshu.compare_summary([]) is None


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_run_fetches_odds_once_and_retries_missing():
    odds = _odds()
    tri = {k: 1 / 120 for k in odds}
    picks = [{"race_key": "20261020_01_05", "race_date": "2026-10-20", "tri_probs": tri},
             {"race_key": "20261020_02_07", "race_date": "2026-10-20", "tri_probs": tri},
             {"race_key": "20261020_03_01", "race_date": "2026-10-20", "tri_probs": tri}]
    saved = {"20261020_03_01": {"race_key": "20261020_03_01", "final_odds": odds, "post_tickets": [{"combo": "x"}]}}
    calls = {"fetch": [], "post": []}

    def fake_sb(method, path, **kw):
        params = dict(kw.get("params") or [])
        if method == "GET" and path == "manshu_chance_picks":
            assert params["result_status"] == "eq.settled"
            return _Resp(picks)
        if method == "GET" and path == manshu_compare.TABLE:
            return _Resp(list(saved.values()))
        if method == "GET" and path == "hindcast_predictions":
            return _Resp([{"race_key": "20261020_01_05", "model_version": "ml-chain-v2-a+p", "tri_probs": tri}])
        if method == "POST":
            calls["post"].extend(kw["json"])
            return _Resp(None)
        raise AssertionError((method, path))

    def fake_fetch(rk):
        calls["fetch"].append(rk)
        if rk == "20261020_02_07":
            raise pre_picks.NoOdds("オッズがまだ出ていません（発売前など）")
        return odds

    orig = manshu_compare._sb
    manshu_compare._sb = fake_sb
    try:
        made = manshu_compare.run("20261017", today="20261022", fetch=fake_fetch,
                                  payouts=lambda ymd: {"20261020_01_05": [("2-1-3", 5000)]})
    finally:
        manshu_compare._sb = orig
    assert made == 1
    assert calls["fetch"] == ["20261020_01_05", "20261020_02_07"]  # 済んだレースは取らない
    by = {r["race_key"]: r for r in calls["post"]}
    assert by["20261020_02_07"]["odds_error"] and "pre_tickets" not in by["20261020_02_07"]
    r = by["20261020_01_05"]
    assert r["final_odds"] == odds and r["pre_stake"] == 1000 and r["post_stake"] == 1000
    assert r["trifecta_actual"] == "2-1-3" and r["is_manshu"] is False


def test_hindcast_skips_pre_models():
    import hindcast

    with tempfile.TemporaryDirectory() as d:
        models = Path(d) / "models"
        models.mkdir()
        for v, last in (("ml-chain-v2-lgb-2y-20261008", "20261007"), ("ml-chain-pre-v2-lgb-2y-20261016", "20261015")):
            (models / f"{v}.json").write_text(json.dumps({"version": v, "info": {"calib_to": last}}))
        orig = hindcast.data_path
        hindcast.data_path = lambda p: Path(d) / p
        try:
            versions = hindcast.ml_versions()
        finally:
            hindcast.data_path = orig
    assert list(versions) == ["ml-chain-v2-lgb-2y-20261008"]
    assert hindcast.ml_version_for("20261017", versions) == "ml-chain-v2-lgb-2y-20261008"
    assert "not.like.ml-chain-pre*" in hindcast.ML_VERSION_FILTER


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
