"""
「1号艇が負けた場合の着順」専用モデル（ml_upset.py）と毎晩の検証（upset_compare.py）の、
データ用リポジトリや Supabase が無くても確かめられる部分。

  python tests/test_upset.py   （pytest でも動く）
"""

from __future__ import annotations

import itertools
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_upset  # noqa: E402
import upset_compare  # noqa: E402

COMBOS = [f"{a}-{b}-{c}" for a, b, c in itertools.permutations(range(1, 7), 3)]


def _uniform_by_first(first):
    """1着確率 {艇番: 確率} → 3連単（1着ごとに20通りへ等分）。"""
    return {c: first.get(int(c[0]), 0.0) / 20 for c in COMBOS}


def test_upset_races():
    f = pd.DataFrame({
        "race_key": ["a"] * 3 + ["b"] * 3 + ["c"] * 2 + ["d"] * 3,
        "lane": [1, 2, 3, 1, 2, 3, 2, 3, 1, 2, 3],
        "finish": [2, 1, 3, 1, 2, 3, 1, 2, None, 1, 2],   # a: 2号艇の勝ち / b: 1号艇の勝ち / c: 1号艇なし / d: 1号艇失格
    })
    assert sorted(ml_upset.upset_races(f)["race_key"].unique()) == ["a", "d"]


def test_combine_keeps_lane1_and_sums_to_one():
    base = _uniform_by_first({1: 0.6, 2: 0.1, 3: 0.1, 4: 0.1, 5: 0.05, 6: 0.05})
    cond = {c: p / 0.4 for c, p in _uniform_by_first({2: 0.1, 3: 0.3, 4: 0.0, 5: 0.0, 6: 0.0}).items()
            if c[0] != "1"}
    out = ml_upset.combine(base, cond)
    assert len(out) == 120
    assert abs(sum(out.values()) - 1) < 1e-4
    assert abs(ml_upset.lane1_win(out) - 0.6) < 1e-4
    assert all(abs(out[c] - base[c]) < 1e-9 for c in COMBOS if c[0] == "1")
    # 1号艇以外が1着の組: 0.4 × 専用モデル（3号艇 3/4、2号艇 1/4）
    first = {a: sum(p for c, p in out.items() if c[0] == str(a)) for a in range(1, 7)}
    assert abs(first[3] - 0.3) < 1e-4 and abs(first[2] - 0.1) < 1e-4 and first[4] == 0
    assert ml_upset.combine({}, cond) is None and ml_upset.combine(base, None) is None


def test_upset_summary():
    cond = {c: p for c, p in _uniform_by_first({2: 0.2, 3: 0.5, 4: 0.2, 5: 0.1}).items() if c[0] != "1"}
    s = ml_upset.upset_summary(cond, _uniform_by_first({1: 0.55, 2: 0.45}))
    assert (s["top1"], s["top2"]) == (3, 2)        # 同じ確率なら内側
    assert round(s["top1_prob"], 3) == 0.5 and round(s["p_lane1_lose"], 3) == 0.45
    assert list(s["first"]) == ["3", "2", "4", "5", "6"]


def test_version_for():
    vers = {"ml-upset-v1-lgb-2y-20261016": "20261015", "ml-upset-v1-lgb-2y-20261023": "20261022"}
    assert ml_upset.version_for("20261017", vers) == "ml-upset-v1-lgb-2y-20261016"
    assert ml_upset.version_for("20261023", vers) == "ml-upset-v1-lgb-2y-20261023"
    assert ml_upset.version_for("20261015", vers) is None


def _odds():
    return {c: 5.0 if c[0] == "1" else (50.0 if sum(map(int, c.split("-"))) % 2 else 150.0) for c in COMBOS}


def test_e_tickets_top2_and_a_excludes_lane1():
    probs = _uniform_by_first({1: 0.5, 2: 0.05, 3: 0.2, 4: 0.15, 5: 0.05, 6: 0.05})
    assert upset_compare.top2_lanes(probs) == [3, 4]
    odds = _odds()
    del odds["3-1-2"]                       # オッズの無い組（欠場など）は買えない
    e = upset_compare.e_tickets(probs, odds)
    assert len(e) == 10 and all(t["combo"][0] in "34" for t in e) and "3-1-2" not in [t["combo"] for t in e]
    assert all(t["combo"][0] == "3" for t in e)   # 確率順なので3号艇1着の組が先
    a = upset_compare.a_tickets(probs, odds)
    assert len(a) == 10 and all(t["combo"][0] != "1" for t in a)


def test_model_stats_and_summary():
    odds = _odds()
    cur = _uniform_by_first({1: 0.5, 2: 0.2, 3: 0.1, 4: 0.1, 5: 0.05, 6: 0.05})
    up = _uniform_by_first({1: 0.5, 2: 0.05, 3: 0.05, 4: 0.3, 5: 0.05, 6: 0.05})
    pays = [("4-1-2", 15000)]
    s_cur, _ = upset_compare.model_stats(cur, odds, pays, 4)
    s_up, t_up = upset_compare.model_stats(up, odds, pays, 4)
    assert s_cur["top1_hit"] is False and s_cur["top2_hit"] is False
    assert s_up["top1_hit"] is True and s_up["top2_hit"] is True
    assert s_up["rank_actual"] < s_cur["rank_actual"]
    assert set(s_up) >= {"A", "E"} and len(t_up["E"]) == 10
    s_no, t_no = upset_compare.model_stats(up, None, pays, 4)   # 確定オッズが無ければ買い目は作らない
    assert "A" not in s_no and t_no == {}
    rows = [{"race_date": "2026-10-20", "winner_lane": 4, "is_manshu": True,
             "variants": {"post": {"current": s_cur, "upset": s_up}}},
            {"race_date": "2026-10-20", "winner_lane": 1, "is_manshu": False,
             "variants": {"post": {"current": s_cur, "upset": s_up}}}]
    out = upset_compare.summary(rows)
    assert [r["確率"] for r in out] == ["今のモデル", "専用モデル"]
    assert out[0]["レース数"] == 2 and out[0]["1号艇が負けた"] == 1
    assert out[0]["1番手が1着"] == 0 and out[1]["1番手が1着"] == 1
    assert out[1]["万舟の順位（平均）"] == s_up["rank_actual"]


def test_retry():
    assert upset_compare.retry(None, "20261017", "20261023")
    assert not upset_compare.retry({"post_tri_probs": {1: 1}, "pre_tri_probs": {1: 1}, "variants": {"x": 1}},
                                   "20261020", "20261023")
    assert upset_compare.retry({"post_tri_probs": None}, "20261020", "20261023")
    assert not upset_compare.retry({"post_tri_probs": None}, "20261001", "20261023")


def test_run_end_to_end():
    """Supabase・特徴量・モデルを差し替えて、毎晩の処理が1行を書くところまで。"""
    import manshu_compare
    import pre_picks

    base = _uniform_by_first({1: 0.5, 2: 0.2, 3: 0.1, 4: 0.1, 5: 0.05, 6: 0.05})
    cond = {c: p for c, p in _uniform_by_first({2: 0.05, 3: 0.05, 4: 0.8, 5: 0.05, 6: 0.05}).items() if c[0] != "1"}
    written = []

    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def fake_sb(method, path, **kw):
        if method == "POST":
            written.extend(kw["json"])
            return R(None)
        if path == "manshu_chance_picks":
            return R([{"race_key": "20261020_01_5", "race_date": "2026-10-20", "tri_probs": base},
                      {"race_key": "20261001_01_5", "race_date": "2026-10-01", "tri_probs": base}])
        if path == upset_compare.TABLE:
            return R([{"race_key": "20261001_01_5", "post_tri_probs": None}])   # 古くて作れなかった行は諦める
        if path == "manshu_aim_compare":
            return R([{"race_key": "20261020_01_5", "final_odds": _odds()}])
        raise AssertionError(path)

    saved = (upset_compare._sb, manshu_compare.hindcast_tri, ml_upset.upset_tri, ml_upset.versions)
    try:
        upset_compare._sb = fake_sb
        manshu_compare.hindcast_tri = lambda keys: {k: ("ml-chain-v2-x+p", base) for k in keys}
        ml_upset.upset_tri = lambda model, feats: {"20261020_01_5": cond}
        ml_upset.versions = lambda pre=False: {("ml-upset-pre-v1-a" if pre else "ml-upset-v1-a"): "20261015"}
        feats = {"20261020_01_5": pd.DataFrame({"race_key": ["20261020_01_5"]})}
        n = upset_compare.run(today="20261023", payouts=lambda d: {"20261020_01_5": [("4-1-2", 15000)]},
                              feature_fn=lambda keys, days: (feats, feats), load_model=lambda v: object())
    finally:
        upset_compare._sb, manshu_compare.hindcast_tri, ml_upset.upset_tri, ml_upset.versions = saved
    assert n == 1 and len(written) == 1
    row = written[0]
    assert row["post_version"] == "ml-upset-v1-a" and row["pre_version"] == "ml-upset-pre-v1-a"
    assert row["post_base_version"] == "ml-chain-v2-x+p" and row["winner_lane"] == 4 and row["is_manshu"]
    assert abs(sum(row["post_tri_probs"].values()) - 1) < 1e-3 and row["post_summary"]["top1"] == 4
    v = row["variants"]["post"]
    assert v["upset"]["top1_hit"] and not v["current"]["top1_hit"] and v["upset"]["E"]["hit"]
    assert "post_base_tri_probs" not in row and row["odds_ready"]
    assert upset_compare.summary([{**row, "race_date": "2026-10-20"}])[1]["1番手が1着"] == 1
    assert pre_picks.VOID_COMBOS  # 払戻の除外に使う


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
