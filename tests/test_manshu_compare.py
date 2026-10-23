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
    assert manshu_compare.done({"final_odds": odds, "post_tickets": [{"combo": "4-1-2"}]}, variants=False)
    assert not manshu_compare.done({"final_odds": None, "post_tickets": [{"combo": "4-1-2"}]})


def test_variants_orders_and_mix():
    odds = _odds()
    # 2号艇1着の組は確率が高い（確率順で先）、5号艇1着の組は確率は低いがオッズを大きくする（期待値順で先）
    odds.update({k: (900.0 if (int(k[0]) + int(k[2]) + int(k[4])) % 2 == 0 else 90.0)
                 for k in odds if k.startswith("5")})
    probs = {k: (0.02 if k.startswith("2") else 0.015 if k.startswith("5") else 0.004) for k in odds}
    probs.update({k: 0.03 for k in odds if k.startswith("1")})  # 1号艇の1着確率 = 20 × 0.03 = 0.6
    row = manshu_compare.compare_row({"race_key": "r", "race_date": "2026-10-20", "tri_probs": probs}, odds,
                                     None, [("2-1-3", 5000)])
    v, t = row["variants"], row["variant_tickets"]
    assert v["post"] == {"error": manshu_compare.MISSING_POST} and t["post"] == {}
    pre = v["pre"]
    assert round(pre["p1"], 2) == 0.6
    for name in "ABCD":
        assert len(t["pre"][name]) == 10 and all(x["combo"][0] != "1" for x in t["pre"][name])
        assert pre[name]["stake"] == 1000
        assert pre[name]["prob_sum"] == round(sum(x["prob"] for x in t["pre"][name]), 5)
    tiers = {n: [x["tier"] for x in t["pre"][n]].count("万舟") for n in "ABCD"}
    assert tiers == {"A": 5, "B": 5, "C": 7, "D": 7}
    assert all(x["combo"][0] == "2" for x in t["pre"]["A"])  # 確率順
    assert all(x["combo"][0] == "5" for x in t["pre"]["B"])  # 期待値順
    # A は今の作り方と同じ買い目
    assert t["pre"]["A"] == row["pre_tickets"]
    assert pre["A"]["hit"] is True and pre["A"]["return"] == 5000 and pre["B"]["hit"] is False
    # 列が無いデータベースでは作らない
    assert "variants" not in manshu_compare.compare_row(
        {"race_key": "r", "race_date": "2026-10-20", "tri_probs": probs}, odds, None, [("2-1-3", 5000)], False)
    # 作り方ごとの比較が無い行は、確定オッズがあっても作り直す
    old = {"final_odds": odds, "post_tickets": [{"combo": "4-1-2"}]}
    assert not manshu_compare.done(old) and manshu_compare.done(old, variants=False)
    full = {"p1": 0.3, "top2_hit": False, **{k: {} for k in manshu.VARIANT_NAMES}}
    assert manshu_compare.done({**old, "variants": {"post": full}})
    # E〜I が無い（A〜D だけの）行も作り直す
    assert not manshu_compare.done({**old, "variants": {"post": {k: v for k, v in full.items() if k < "E"}}})


def _vside(p1, hit_a, prob=0.1, ret=15000, top2_hit=False):
    s = {"p1": p1, "error": None, "top2": [2, 3], "top2_hit": top2_hit}
    for n in manshu.VARIANT_NAMES:
        hit = hit_a and n == "A"
        s[n] = {"hit": hit, "stake": 1000, "return": ret if hit else 0, "prob_sum": prob}
    return s


def test_variant_summary_same_races_and_skip():
    rows = [
        {"race_date": "2026-10-20", "is_manshu": True, "variants": {"pre": _vside(0.6, True), "post": _vside(0.3, True)}},
        {"race_date": "2026-10-20", "is_manshu": False,
         "variants": {"pre": _vside(0.2, False), "post": _vside(0.55, False)}},
        # 展示後版がまだ無いレースは数えない
        {"race_date": "2026-10-21", "is_manshu": True,
         "variants": {"pre": _vside(0.2, True), "post": {"error": manshu_compare.MISSING_POST}}},
        {"race_date": "2026-10-21", "is_manshu": True},
    ]
    out = {(r["版"], r["見送り"], r["作り方"][0]): r for r in manshu.variant_summary(rows)}
    assert len(out) == 2 * 2 * len(manshu.VARIANT_NAMES)
    a = out[("朝版", "なし", "A")]
    assert (a["レース数"], a["的中数"], a["的中率"], a["回収率"], a["万舟の的中"], a["確率の合計"]) == (2, 1, 0.5, 7.5, 1, 0.2)
    a = out[("朝版", "あり", "A")]  # 1号艇0.6のレースを見送る
    assert (a["レース数"], a["的中数"], a["回収率"]) == (1, 0, 0.0)
    a = out[("展示後版", "あり", "A")]  # 展示後版は展示後の1号艇の確率で見送る
    assert (a["レース数"], a["的中数"], a["回収率"], a["万舟の的中"]) == (1, 1, 15.0, 1)
    assert out[("展示後版", "なし", "B")]["的中数"] == 0
    assert manshu.variant_summary(rows[2:]) == []
    # A〜D だけの古い行は、作り直すまで数えない
    old = {"race_date": "2026-10-20", "is_manshu": True,
           "variants": {s: {k: v for k, v in _vside(0.2, True).items() if k not in "EFGHI"} for s in ("pre", "post")}}
    assert manshu.variant_summary([old]) == []


def test_top2_variants():
    odds = _odds()
    odds.update({"3-2-4": 8.0, "3-4-2": 12.0})  # 30倍未満の組
    first = {1: 0.40, 2: 0.10, 3: 0.25, 4: 0.20, 5: 0.03, 6: 0.02}
    probs = {}
    for k in odds:
        rest = [x for x in range(1, 7) if x != int(k[0])]
        probs[k] = first[int(k[0])] / 20 * (1 - 0.1 * rest.index(int(k[2])) - 0.01 * rest.index(int(k[4])))
    assert manshu.top2_lanes(probs) == [3, 4]
    assert manshu.top2_lanes(_tri({1: 0.4, 2: 0.2, 5: 0.2, 3: 0.2})) == [2, 3]  # 同じ確率なら内側
    row = manshu_compare.compare_row({"race_key": "r", "race_date": "2026-10-20", "tri_probs": probs}, odds,
                                     ("ml-chain-v2-x+p", probs), [("4-1-2", 9000)])
    v, t = row["variants"]["pre"], row["variant_tickets"]["pre"]
    assert v["top2"] == [3, 4] and v["top2_hit"] is True
    for name, n in {"E": 10, "F": 15, "G": 20, "H": 10, "I": 20}.items():
        tk = t[name]
        assert len(tk) == n and {x["combo"][0] for x in tk} <= {"3", "4"}
        assert [x["prob"] for x in tk] == sorted((x["prob"] for x in tk), reverse=True)
        assert v[name]["stake"] == 100 * n and v[name]["prob_sum"] == round(sum(x["prob"] for x in tk), 5)
    # オッズの条件なしは30倍未満も買う、H・I は30倍以上だけ
    assert {x["combo"]: x["tier"] for x in t["G"]}["3-2-4"] == "30倍未満"
    assert all(x["odds"] >= 30 for x in t["H"] + t["I"])
    assert [x["combo"] for x in t["H"]] == [x["combo"] for x in t["G"] if x["odds"] >= 30][:10]
    # E は G の上位10点
    assert t["E"] == t["G"][:10]
    assert v["G"]["hit"] is True and v["G"]["return"] == 9000
    # 実際の1着が2艇以外なら外れ
    row = manshu_compare.compare_row({"race_key": "r", "race_date": "2026-10-20", "tri_probs": probs}, odds,
                                     None, [("1-3-4", 1200)])
    assert row["variants"]["pre"]["top2_hit"] is False and row["variants"]["pre"]["G"]["hit"] is False


def test_top2_summary():
    rows = [
        {"race_date": "2026-10-20", "is_manshu": True,
         "variants": {"pre": _vside(0.6, True, top2_hit=True), "post": _vside(0.3, True, top2_hit=True)}},
        {"race_date": "2026-10-20", "is_manshu": False,
         "variants": {"pre": _vside(0.2, False), "post": _vside(0.55, False, top2_hit=True)}},
        {"race_date": "2026-10-21", "is_manshu": True,
         "variants": {"pre": _vside(0.2, True, top2_hit=True), "post": {"error": manshu_compare.MISSING_POST}}},
    ]
    out = {(r["版"], r["見送り"]): r for r in manshu.top2_summary(rows)}
    assert (out[("朝版", "なし")]["レース数"], out[("朝版", "なし")]["2艇のどちらかが1着"]) == (2, 1)
    assert (out[("朝版", "あり")]["レース数"], out[("朝版", "あり")]["割合"]) == (1, 0.0)
    assert out[("展示後版", "なし")]["割合"] == 1.0 and out[("展示後版", "あり")]["レース数"] == 1
    assert manshu.top2_summary(rows[2:]) == []


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
                                  payouts=lambda ymd: {"20261020_01_05": [("2-1-3", 5000)],
                                                       "20261020_03_01": [("3-1-2", 8000)]})
    finally:
        manshu_compare._sb = orig
    assert made == 2
    assert calls["fetch"] == ["20261020_01_05", "20261020_02_07"]  # 確定オッズが済んだレースは取らない
    by = {r["race_key"]: r for r in calls["post"]}
    assert by["20261020_02_07"]["odds_error"] and "pre_tickets" not in by["20261020_02_07"]
    r = by["20261020_01_05"]
    assert r["final_odds"] == odds and r["pre_stake"] == 1000 and r["post_stake"] == 1000
    assert r["trifecta_actual"] == "2-1-3" and r["is_manshu"] is False
    assert r["variants"]["pre"]["A"]["stake"] == 1000 and r["variants"]["post"]["D"]["stake"] == 1000
    assert r["variants"]["pre"]["G"]["stake"] == 2000 and r["variants"]["post"]["F"]["stake"] == 1500
    # 済んだ行（03_01）も作り方ごとの比較が無いので、保存済みの確定オッズで作り直す（取り直さない）
    assert by["20261020_03_01"]["final_odds"] == odds and "variants" in by["20261020_03_01"]


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
