"""
検証タブの集計（validation_view.py・pre_picks_view の期間の区切り）の、Supabase が無くても確かめられる部分。

  python tests/test_validation_view.py   （pytest でも動く）
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manshu  # noqa: E402
import pre_picks_view  # noqa: E402
import validation_view as vv  # noqa: E402
from result_tracker import RESULT_COLUMNS  # noqa: E402

TODAY = date(2026, 10, 22)


def _result(race_date, version, first_hit, cand_hit, stake=1000, payout=0, **kw):
    row = {c: None for c in RESULT_COLUMNS}
    row.update(race_key=f"{race_date}_{version}_{kw.pop('n', 1)}", race_date=race_date, model_version=version,
               predicted_first_hit=first_hit, hit_any_ticket=cand_hit, hit_top_ticket=False,
               candidate_hit=cand_hit, total_stake=stake, payout=payout, p1_prob=0.5, candidate_count=9, **kw)
    return row


def _results():
    return pd.DataFrame([
        _result("2026-10-21", "ml-chain-v2-lgb-2y-20261019", True, True, payout=3000, n=1),
        _result("2026-10-20", "ml-chain-v2-lgb-2y-20261019", False, False, n=2),
        _result("2026-10-10", "ml-chain-v2-lgb-2y-20261008", True, False, n=3),
        _result("2026-10-10", "position-v3-20260914", True, True, payout=9000, n=4),
        _result("2026-09-20", "position-v3-20260914", False, True, payout=2000, n=5),
        _result("2026-08-20", None, False, False, n=6),
    ])


def test_summary_uses_production_model_only():
    rows = {r["期間"]: r for r in vv.summary_rows(vv.production_rows(_results()), TODAY)}
    assert rows["直近7日"]["レース数"] == 2
    assert rows["直近7日"]["本命的中率"] == 0.5
    assert rows["直近7日"]["回収率"] == 1.5
    assert rows["切り替え後通算"]["レース数"] == 3
    assert abs(rows["切り替え後通算"]["候補内的中率"] - 1 / 3) < 1e-9
    t = vv.summary_table(vv.summary_rows(vv.production_rows(_results()), TODAY))
    assert list(t.columns) == ["項目", "直近7日", "直近30日", "通算"]
    assert t.iloc[0]["通算"] == "3R" and t.iloc[3]["直近7日"] == "150.0%"
    v = vv.version_counts(vv.production_rows(_results()))
    assert list(v["版"]) == ["ml-chain-v2-lgb-2y-20261008", "ml-chain-v2-lgb-2y-20261019"]


def test_second_favorite_split_by_date_and_model():
    rows = {r["期間"].split("（")[0]: r for r in vv.second_favorite_rows(_results())}
    assert rows["切り替え前"]["レース数"] == 1          # 9/8〜10/2（8/20 は入れない）
    assert rows["切り替え後・合計"]["レース数"] == 4
    assert rows["切り替え後・今までのモデル"]["レース数"] == 1
    assert rows["切り替え後・新しいモデル"]["レース数"] == 3
    assert rows["切り替え後・今までのモデル"]["回収率"] == 9.0


def test_shown_probability_prefers_calibrated():
    df = pd.DataFrame({"hit_probability": [0.4, 0.5], "hit_probability_calibrated": [0.3, None]})
    assert list(vv.with_shown_probability(df)["hit_probability_shown"]) == [0.3, 0.5]


def _hc(d, model, paired, cand, lost, lost_hit, races=None, manshu_races=0):
    return {"race_date": d, "model": model, "races": races or paired, "manshu_races": manshu_races,
            "paired_races": paired, "paired_favorite_hits": cand, "paired_candidate_hits": cand,
            "paired_lane1_lost": lost, "paired_lane1_lost_candidate_hits": lost_hit}


def test_hindcast_period_and_daily():
    rows = [_hc("2026-10-20", "new", 100, 50, 40, 10), _hc("2026-10-20", "old", 100, 45, 40, 6),
            _hc("2026-10-01", "old", 0, 0, 0, 0, races=140, manshu_races=28)]
    t = {(r["期間"], r["モデル"]): r for r in vv.hindcast_period_rows(rows, TODAY)}
    assert t[("直近7日", "新モデル")]["候補内的中率"] == 0.5
    assert t[("直近7日", "旧モデル")]["1号艇負け 候補内的中率"] == 0.15
    assert ("全期間", "旧モデル") in t and t[("全期間", "旧モデル")]["レース数"] == 100
    daily = vv.hindcast_daily_frame(rows)
    assert list(daily.index) == ["2026-10-20"] and daily.loc["2026-10-20", "旧モデル"] == 0.45
    assert vv.manshu_rate_on(rows, {"2026-10-01"}) == 0.2
    assert vv.manshu_rate_on(rows, {"2026-09-01"}) is None


def test_pre_picks_periods_match_prediction_tab():
    rows = [
        {"race_date": "2026-10-21", "pre_hit": True, "pre_stake": 2000, "pre_return": 5000,
         "post_tickets": None, "trifecta_payout": 2500, "post_flag": False},
        {"race_date": "2026-10-21", "pre_hit": False, "pre_stake": 2000, "pre_return": 0,
         "post_tickets": [{"combo": "1-2-3"}], "post_hit": True, "post_stake": 2000, "post_return": 3000,
         "trifecta_payout": 1500, "post_flag": True},
        {"race_date": "2026-09-01", "pre_hit": False, "pre_stake": 2000, "pre_return": 0,
         "post_tickets": None, "trifecta_payout": 9000, "post_flag": False},
    ]
    t = {r["期間"]: r for r in vv.pre_picks_period_rows(rows, TODAY)}
    assert t["直近7日"]["的中"] == "2/2"
    assert t["全期間"]["的中"] == "2/3"
    # 予想タブの成績（period_rows + summarize）と同じ数字
    tab = {r["期間"]: r for r in pre_picks_view.period_rows(rows, TODAY, pre_picks_view.summarize)}
    assert t["全期間"]["的中率"] == tab["全期間"]["的中率（使った買い目）"]
    daily = pre_picks_view.daily_hits(rows)
    assert daily[0] == {"日付": "2026-10-21", "レース数": 2, "的中数": 2, "的中率": 1.0}


def test_manshu_periods_use_same_cutoff():
    rows = [{"race_date": "2026-10-15", "trifecta_payout": 12000, "is_manshu": True},
            {"race_date": "2026-10-14", "trifecta_payout": 800, "is_manshu": False}]
    t = {r["期間"]: r for r in pre_picks_view.period_rows(rows, TODAY, manshu.summarize)}
    assert t["直近7日"]["レース数"] == 1 and t["直近30日"]["レース数"] == 2


def test_button_compare():
    df = pd.DataFrame([
        {"predicted_first_hit": True, "candidate_hit": True, "total_stake": 2000, "payout": 4000,
         "claude_first_hit": False, "claude_candidate_hit": True, "claude_total_stake": 2000, "claude_payout": 1000,
         "mix_first_hit": True, "mix_candidate_hit": None, "mix_total_stake": None, "mix_payout": None},
        {"predicted_first_hit": False, "candidate_hit": False, "total_stake": 2000, "payout": 0,
         "claude_first_hit": True, "claude_candidate_hit": False, "claude_total_stake": 2000, "claude_payout": 0,
         "mix_first_hit": False, "mix_candidate_hit": True, "mix_total_stake": 2000, "mix_payout": 6000},
    ])
    t = {r["予想"]: r for r in vv.button_compare_rows(df)}
    assert t["モデルのみ"]["本命的中率"] == "50.0%（1/2）" and t["モデルのみ"]["回収率"] == "100.0%"
    assert t["Claudeのみ"]["回収率"] == "25.0%"
    assert t["両方の平均"]["候補内的中率"] == "100.0%（1/1）" and t["両方の平均"]["回収率"] == "300.0%"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
