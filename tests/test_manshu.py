"""
「💥 万舟チャンス」（manshu.py・pre_picks.py・manshu_view.py）の、データ用リポジトリが無くても確かめられる部分。

  python tests/test_manshu.py   （pytest でも動く）
"""

from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import manshu  # noqa: E402
import pre_picks  # noqa: E402


def _feats(race_keys, seed=0):
    """ml_features と同じ列を持つ、1艇1行の小さな表。"""
    rng = np.random.default_rng(seed)
    rows = []
    for rk in race_keys:
        for ln in range(1, 7):
            r = {"race_key": rk, "lane": float(ln), "race_date": rk[:8], "jcd": rk[9:11], "race_no": 5}
            for c in manshu.LANE_COLS_COMMON + manshu.LANE_COLS_POST:
                r[c] = float(rng.normal())
            for c in manshu.RACE_COLS_COMMON + manshu.RACE_COLS_POST:
                if c not in r:
                    r[c] = float(rng.integers(0, 5))
            rows.append(r)
    f = pd.DataFrame(rows)
    f.loc[f["race_key"] == race_keys[0], "class_ord"] = [4.0, 3.0, 2.0, 1.0, 4.0, np.nan]
    return f


def _chain(race_keys, seed=0):
    rng = np.random.default_rng(seed)
    first, tri = [], []
    for rk in race_keys:
        p = rng.dirichlet(np.ones(6))
        first += [{"race_key": rk, "lane": ln, "p_first": float(p[ln - 1])} for ln in range(1, 7)]
        q = rng.dirichlet(np.ones(120))
        tri += [{"race_key": rk, "combo": f"{a}-{b}-{c}", "prob": float(q[i])}
                for i, (a, b, c) in enumerate(itertools.permutations(range(1, 7), 3))]
    return pd.DataFrame(first), pd.DataFrame(tri)


def test_one_race_equals_batch():
    """アプリは1レースずつ、朝は全レースまとめて計算する。どちらでも同じ特徴量になる。"""
    keys = ["20261018_01_5", "20261018_02_5", "20261018_03_5"]
    f = _feats(keys)
    first, tri = _chain(keys)
    for post in (False, True):
        batch = manshu.race_features(f, post).join(manshu.chain_summary(first, tri))
        for rk in keys:
            one = manshu.race_features(f[f["race_key"] == rk], post).join(
                manshu.chain_summary(first[first["race_key"] == rk], tri[tri["race_key"] == rk]))
            pd.testing.assert_frame_equal(one.loc[[rk]], batch.loc[[rk]][one.columns], check_dtype=False)


def test_features_basic():
    keys = ["20261018_01_5"]
    f = _feats(keys)
    first, tri = _chain(keys)
    x = manshu.race_features(f, post=False)
    assert "exhibition_time_l1" not in x.columns and "wind_speed" not in x.columns  # 展示前版に展示・気象は入らない
    assert x.loc[keys[0], "n_top_class"] == 2  # 級別の最上位（4）が2艇（欠けた艇は数えない）
    assert x.loc[keys[0], "jcd"] == 1
    c = manshu.chain_summary(first, tri)
    p = first["p_first"].to_numpy()
    assert np.isclose(c.loc[keys[0], "c_p1_max"], p.max())
    assert np.isclose(c.loc[keys[0], "c_p1_entropy"], -(p * np.log(p)).sum())
    assert np.isclose(c.loc[keys[0], "c_lane1_out3"],
                      tri.loc[~tri["combo"].str.contains("1"), "prob"].sum())


def test_longshot_tickets():
    keys = ["20261018_01_5"]
    _, tri = _chain(keys)
    longs = manshu.longshot_tickets(tri)[keys[0]]
    assert len(longs) == manshu.LONGSHOT_N
    assert all(not t["combo"].startswith("1") for t in longs)
    probs = [t["prob"] for t in longs]
    assert probs == sorted(probs, reverse=True)
    best = tri[~tri["combo"].str.startswith("1")].sort_values("prob").iloc[-1]["combo"]
    assert longs[0]["combo"] == best


def test_predict_frame_needs_all_features():
    class Bst:
        def feature_name(self):
            return ["c_p1_max", "no_such_column"]

    keys = ["20261018_01_5"]
    first, tri = _chain(keys)
    try:
        manshu.predict_frame(Bst(), _feats(keys), first, tri, post=False)
    except RuntimeError as e:
        assert "no_such_column" in str(e)
    else:
        raise AssertionError("足りない特徴量で予想してしまった")


def test_select_and_rows():
    c = pd.DataFrame({
        "race_key": [f"20261018_01_{i}" for i in range(1, 15)], "race_date": "2026-10-18", "jcd": "01",
        "venue": "桐生", "race_no": list(range(1, 15)), "deadline": [f"{9 + i // 2}:{(i % 2) * 30:02d}" for i in range(14)],
        "model_version": "ml-chain-pre", "favorite_lane": 1, "favorite_prob": 0.5,
        "p_first": [[0.5, 0.1, 0.1, 0.1, 0.1, 0.1]] * 14, "tickets": [[{"combo": "1-2-3", "stake": 2000}]] * 14,
        "manshu_prob": [0.1 + i / 100 for i in range(13)] + [np.nan],
        "longshot_tickets": [[{"combo": "2-1-3", "prob": 0.05}]] * 14,
    })
    c.attrs["manshu_version"] = "manshu-v1"
    m = pre_picks.select_manshu(c)
    assert len(m) == manshu.PICK_COUNT and list(m["pick_rank"]) == list(range(1, 11))
    assert m.iloc[0]["race_no"] == 13 and m["manshu_prob"].notna().all()
    m.attrs = dict(c.attrs)
    rows = pre_picks.manshu_rows(m)
    assert rows[0]["manshu_model_version"] == "manshu-v1" and rows[0]["pick_rank"] == 1
    import json
    json.dumps(rows)  # Supabase に送れる形
    # 確率の列が無い（万舟チャンスが出せなかった）ときは選ばない
    assert pre_picks.select_manshu(c.drop(columns=["manshu_prob"])).empty


def test_add_manshu_failure_keeps_picks():
    """万舟モデルが無くても厳選10の候補はそのまま（理由だけ残る）。"""
    c = pd.DataFrame({"race_key": ["20261018_01_1"], "hit_probability": [0.6]})
    orig = manshu.predict_with_chain
    manshu.predict_with_chain = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("モデルが無い"))
    try:
        out = pre_picks.add_manshu(c, model=None, feats=None)
    finally:
        manshu.predict_with_chain = orig
    assert "manshu_prob" not in out.columns and "モデルが無い" in out.attrs["manshu_error"]
    assert len(out) == 1


def test_manshu_result_fields():
    row = {"tickets": [{"combo": "1-2-3", "stake": 1500}, {"combo": "1-3-2", "stake": 500}],
           "longshot_tickets": [{"combo": "4-1-2", "prob": 0.02}, {"combo": "2-1-3", "prob": 0.03}],
           "post_tickets": None, "post_longshot_tickets": [{"combo": "4-1-2", "prob": 0.02}]}
    out = pre_picks.manshu_result_fields(row, [("4-1-2", 15230)])
    assert out["result_status"] == "settled" and out["is_manshu"] is True
    assert out["main_hit"] is False and out["main_stake"] == 2000 and out["main_return"] == 0
    assert out["longshot_hit"] is True and out["longshot_stake"] == 200 and out["longshot_return"] == 15230
    assert out["post_longshot_return"] == 15230 and "post_main_hit" not in out
    small = pre_picks.manshu_result_fields(row, [("1-2-3", 9990)])
    assert small["is_manshu"] is False and small["main_return"] == 149850
    assert pre_picks.manshu_result_fields(row, None)["result_status"] == "void"
    assert pre_picks.manshu_result_fields(row, [("特払い", 70)])["result_status"] == "void"


def test_summarize():
    rows = [
        {"race_date": "2026-10-18", "trifecta_payout": 15230, "is_manshu": True, "main_hit": False,
         "main_stake": 2000, "main_return": 0, "longshot_hit": True, "longshot_stake": 1000, "longshot_return": 15230},
        {"race_date": "2026-10-19", "trifecta_payout": 1200, "is_manshu": False, "main_hit": True,
         "main_stake": 2000, "main_return": 3000, "longshot_hit": False, "longshot_stake": 1000, "longshot_return": 0},
    ]
    s = manshu.summarize(rows)
    assert s["レース数"] == 2 and s["万舟率"] == 0.5
    assert s["本線 回収率"] == 0.75 and s["穴寄り 回収率"] == 15230 / 2000 and s["穴寄り 的中率"] == 0.5
    assert manshu.summarize([]) is None


def test_view_helpers():
    import manshu_view

    marks = {"20261018_01_5": {"jcd": "01", "race_no": 5}, "20261018_01_9": {"jcd": "1", "race_no": 9},
             "20261018_24_2": {"jcd": "24", "race_no": 2}}
    assert manshu_view.venue_counts(marks) == {"01": 2, "24": 1}
    assert manshu_view.race_nos(marks, "01") == {5, 9}
    final = pd.DataFrame({"lane": range(1, 7), "p_first": [0.5, 0.2, 0.1, 0.1, 0.05, 0.05]})
    longs = manshu_view.longshot_from_final(final)
    assert len(longs) == manshu.LONGSHOT_N and all(t["combo"][0] != "1" for t in longs)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
