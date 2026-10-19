"""
万舟狙いの買い目・一覧の当日の結果（manshu.py・list_results.py・pre_picks.py・各一覧の表示）の、
データ用リポジトリや Supabase が無くても確かめられる部分。

  python tests/test_manshu_aim.py   （pytest でも動く）
"""

from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import list_results  # noqa: E402
import manshu  # noqa: E402
import pre_picks  # noqa: E402

PROBS = {"1-2-3": 0.20, "4-1-2": 0.03, "2-3-1": 0.05, "1-5-6": 0.01, "3-1-2": 0.04, "5-6-4": 0.002}
ODDS = {"1-2-3": 4.5, "4-1-2": 150.0, "2-3-1": 120.0, "1-5-6": 300.0, "3-1-2": 99.9, "5-6-4": 2500.0}


def test_aim_tickets_a():
    t = manshu.aim_tickets(PROBS, ODDS, method="A")
    assert [x["combo"] for x in t] == ["2-3-1", "4-1-2", "1-5-6", "5-6-4"]  # 100倍以上だけ・確率順（99.9倍は外す）
    assert t[0]["stake"] == 100 and t[0]["payout"] == 12000 and t[0]["odds"] == 120.0


def test_aim_tickets_b_prefers_outer():
    t = manshu.aim_tickets(PROBS, ODDS, method="B")
    # 4〜6号艇が1着か2着の組（4-1-2・1-5-6・5-6-4）を先に、その中は確率順。残り（2-3-1）で埋める
    assert [x["combo"] for x in t] == ["4-1-2", "1-5-6", "5-6-4", "2-3-1"]
    assert manshu.aim_tickets(PROBS, ODDS, method="B", n=2) == t[:2]


def test_aim_tickets_mix_mid_and_high():
    probs = {f"{a}-{b}-{c}": p for (a, b, c), p in zip(
        [(1, 2, 3), (1, 3, 2), (2, 1, 3), (3, 1, 2), (1, 2, 4), (1, 4, 2), (4, 1, 2), (5, 1, 2), (6, 1, 2),
         (2, 3, 1), (4, 5, 6), (6, 5, 4), (5, 6, 4)],
        [0.2, 0.1, 0.06, 0.05, 0.04, 0.035, 0.03, 0.02, 0.015, 0.012, 0.004, 0.003, 0.002])}
    odds = {"1-2-3": 4.0, "1-3-2": 8.0, "2-1-3": 31.0, "3-1-2": 45.0, "1-2-4": 30.0, "1-4-2": 60.0,
            "4-1-2": 99.9, "5-1-2": 150.0, "6-1-2": 210.0, "2-3-1": 100.0, "4-5-6": 3000.0, "6-5-4": 2500.0,
            "5-6-4": 900.0}
    t = manshu.aim_tickets(probs, odds, method="MIX", mix={"中穴": 5, "万舟": 5}, order="prob")
    assert [x["combo"] for x in t] == ["2-1-3", "3-1-2", "1-2-4", "1-4-2", "4-1-2",
                                       "5-1-2", "6-1-2", "2-3-1", "4-5-6", "6-5-4"]
    assert [x["tier"] for x in t] == ["中穴"] * 5 + ["万舟"] * 5  # 30倍ちょうどは中穴・100倍ちょうどは万舟
    ev = manshu.aim_tickets(probs, odds, method="MIX", mix={"中穴": 5, "万舟": 5}, order="ev")
    assert ev[5]["combo"] == "4-5-6"  # 万舟の中で確率×オッズが最大（0.004×3000=12）
    # 中穴が足りないときは万舟で埋める（合計10点）
    few = {k: v for k, v in odds.items() if not 30 <= v < 100 or k == "2-1-3"}
    t2 = manshu.aim_tickets(probs, few, method="MIX", mix={"中穴": 5, "万舟": 5})
    assert len(t2) == 7 and [x["tier"] for x in t2].count("中穴") == 1  # 中穴1組＋万舟6組（全部）
    assert manshu.odds_tier(29.9) is None and manshu.odds_tier(30) == "中穴" and manshu.odds_tier(100) == "万舟"
    assert manshu.aim_method_name() == "MIX5+5-prob"


def test_aim_tickets_missing_odds():
    assert manshu.aim_tickets(PROBS, {}) == []
    assert manshu.aim_tickets(PROBS, {"1-2-3": float("nan")}) == []
    assert manshu.is_outer_top2("1-4-2") and not manshu.is_outer_top2("1-2-4")


def test_odds_dict_from_frame():
    import pandas as pd

    o = pd.DataFrame({"combo": ["1-2-3", "4-5-6"], "odds": ["4.5", "1234.0"]})
    assert manshu.odds_dict(o) == {"1-2-3": 4.5, "4-5-6": 1234.0}
    assert manshu.odds_dict(None) == {} and manshu.odds_dict(pd.DataFrame(columns=["combo", "odds"])) == {}


def test_aim_result_fields():
    aim = manshu.aim_tickets(PROBS, ODDS)
    row = {"tickets": [{"combo": "1-2-3", "stake": 2000}], "longshot_tickets": [], "aim_tickets": aim}
    out = pre_picks.manshu_result_fields(row, [("4-1-2", 15230)])
    assert out["aim_hit"] is True and out["aim_stake"] == 500 and out["aim_return"] == 15230
    assert out["is_manshu"] is True and out["main_hit"] is False
    assert "aim_hit" not in pre_picks.manshu_result_fields({"tickets": []}, [("4-1-2", 15230)])


def test_summary_includes_aim():
    rows = [{"race_date": "2026-10-18", "trifecta_payout": 15230, "is_manshu": True, "aim_hit": True,
             "aim_stake": 1000, "aim_return": 15230},
            {"race_date": "2026-10-18", "trifecta_payout": 800, "is_manshu": False, "aim_hit": None,
             "aim_stake": None, "aim_return": None}]
    s = manshu.summarize(rows)
    assert s["万舟狙い レース数"] == 1 and s["万舟狙い 的中率"] == 1.0 and abs(s["万舟狙い 回収率"] - 15.23) < 1e-9


class _FakeSb:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        table = path.split("?")[0]

        class R:
            def __init__(s, data):
                s.data = data

            def json(s):
                return s.data
        if method == "GET":
            return R(self.rows.get(table, []))
        return R(None)


def _rows():
    return {
        "pre_exhibition_picks": [{"race_key": "20261018_01_5", "race_date": "2026-10-18", "deadline": "11:00",
                                  "jcd": "01", "tickets": [{"combo": "1-2-3", "stake": 2000}]}],
        "manshu_chance_picks": [
            {"race_key": "20261018_01_5", "race_date": "2026-10-18", "deadline": "11:00", "jcd": "01",
             "tickets": [], "longshot_tickets": [], "aim_tickets": [{"combo": "1-2-3", "stake": 100}]},
            {"race_key": "20261018_02_9", "race_date": "2026-10-18", "deadline": "15:30", "jcd": "02",
             "tickets": [], "longshot_tickets": []}],
    }


def test_due_races_waits_after_deadline():
    now = dt.datetime(2026, 10, 18, 11, 25, tzinfo=list_results.JST)
    assert list_results.due_races(_rows(), now) == ["20261018_01_5"]  # 締切20分後を過ぎたレースだけ・重複なし
    assert list_results.due_races(_rows(), now - dt.timedelta(minutes=10)) == []


def test_on_official_result_writes_both_lists():
    sb = _FakeSb(_rows())
    orig = list_results._sb
    list_results._sb = sb
    try:
        list_results.on_official_result("20261018_01_5", {"trifecta": "1-2-3", "trifecta_payout_per_100": 1230})
    finally:
        list_results._sb = orig
    patches = [(p.split("?")[0], j) for m, p, j in sb.calls if m == "PATCH"]
    tables = [t for t, _ in patches]
    assert tables.count("pre_exhibition_picks") == 1 and tables.count("manshu_chance_picks") == 2  # GETの戻りを全部書く
    pre = dict(patches)["pre_exhibition_picks"]
    assert pre["result_status"] == "settled" and pre["pre_hit"] is True and pre["pre_return"] == 24600
    assert all("result_at=is.null" in p for m, p, _ in sb.calls if m == "PATCH")  # 記録済みは上書きしない


def test_sync_pending_fetches_only_due_and_skips_not_ready():
    rows = _rows()
    sb = _FakeSb(rows)
    fetched = []

    def fetch(date, jcd, rno):
        fetched.append((date, jcd, rno))
        raise ValueError("まだ")
    orig = list_results._sb
    list_results._sb = sb
    try:
        list_results.sync_pending(fetch=fetch, now=dt.datetime(2026, 10, 18, 11, 25, tzinfo=list_results.JST))
    finally:
        list_results._sb = orig
    assert fetched == [("20261018", "01", 5)]
    assert not any(m == "PATCH" for m, _, _ in sb.calls)


def test_list_states_and_day_summaries():
    import manshu_view
    import pre_picks_view

    d = dt.date(2020, 1, 1)  # 締切はすべて過去
    pre_rows = [
        {"jcd": "01", "deadline": "10:00", "result_status": "settled", "trifecta_actual": "1-2-3",
         "trifecta_payout": 1230, "tickets": [{"combo": "1-2-3", "stake": 2000}], "pre_hit": True,
         "pre_stake": 2000, "pre_return": 24600},
        {"jcd": "01", "deadline": "11:00", "result_status": None},
        {"jcd": "03", "deadline": "12:00", "result_status": None},
        {"jcd": "01", "deadline": "13:00", "result_status": "void"},
    ]
    assert [pre_picks_view.result_state(d, r, {"03"}) for r in pre_rows] == ["settled", "waiting", "void", "void"]
    assert "○" in pre_picks_view.result_line(pre_rows[0], "settled")
    assert pre_picks_view.result_line(pre_rows[1], "waiting") == "結果待ち"
    assert pre_picks_view.result_line(pre_rows[2], "void") == "結果: 中止"
    s = pre_picks_view.day_summary(pre_rows, d, {"03"})
    assert (s["settled"], s["hits"], s["return"], s["waiting"], s["void"]) == (1, 1, 24600, 1, 2)
    future = dt.date(2099, 1, 1)
    assert pre_picks_view.result_state(future, pre_rows[1]) is None  # 締切前は何も出さない

    m_rows = [
        {"jcd": "01", "deadline": "10:00", "result_status": "settled", "trifecta_actual": "4-1-2",
         "trifecta_payout": 15230, "is_manshu": True, "aim_tickets": [{"combo": "4-1-2"}], "aim_hit": True,
         "aim_stake": 1000, "aim_return": 15230},
        {"jcd": "01", "deadline": "11:00", "result_status": "settled", "trifecta_actual": "1-2-3",
         "trifecta_payout": 800, "is_manshu": False, "aim_tickets": None},
    ]
    line = manshu_view.manshu_result_line(m_rows[0], "settled")
    assert "💥 万舟" in line and "○" in line and "15,230円" in line
    assert "表示した買い目: なし" in manshu_view.manshu_result_line(m_rows[1], "settled")
    ms = manshu_view.manshu_day_summary(m_rows, d)
    # 買い目を表示しなかったレースは的中の母数に入れない。実際に万舟だった数も出す
    assert (ms["settled"], ms["with_tickets"], ms["hits"], ms["return"], ms["stake"]) == (2, 1, 1, 15230, 1000)
    assert "1 / 2" in ms["extra"] and "万舟" in ms["extra"]
    # 展示前厳選10と万舟チャンスで、結果の行の作りは同じ（違うのは万舟の一言だけ）
    pre_line = pre_picks_view.result_line(pre_rows[0], "settled")
    assert pre_line.startswith("結果: 3連単 1-2-3（1,230円）") and "表示した買い目 ○" in pre_line


def test_aim_probs_prefer_post_and_post_flag_fields():
    import manshu_view

    r = {"tri_probs": {"1-2-3": 0.1}, "post_tri_probs": None}
    assert manshu_view.aim_probs(r) == ({"1-2-3": 0.1}, "展示前")
    r["post_tri_probs"] = {"1-2-3": 0.2}
    assert manshu_view.aim_probs(r) == ({"1-2-3": 0.2}, "展示後")
    assert manshu_view.aim_result({"aim_tickets": None}) is None
    assert manshu_view.aim_result({"aim_tickets": [{"combo": "1-2-3"}], "aim_stake": 1000, "aim_hit": False,
                                   "aim_return": 0}) == (False, 1000, 0)
    txt = manshu_view.aim_text([{"combo": "2-1-3", "odds": 45.2, "tier": "中穴", "stake": 100, "payout": 4520}])
    assert txt == "【中穴】2-1-3 45.2倍"  # 組み合わせとオッズだけ（払戻額は出さない）
    import pre_picks_view

    assert pre_picks_view.favorite_line({"favorite_lane": 1, "favorite_prob": 0.5, "post_favorite_lane": 3}) \
        == "本命: 1号艇（1着確率 50%） → 展示後 3号艇"
    assert pre_picks_view.favorite_line({"favorite_lane": None}) is None


def test_morning_rows_keep_tri_probs_and_fallback():
    import pandas as pd

    c = pd.DataFrame({
        "race_key": ["20261018_01_1"], "race_date": "2026-10-18", "jcd": "01", "venue": "桐生", "race_no": [1],
        "deadline": ["10:00"], "model_version": "v", "favorite_lane": 1, "favorite_prob": 0.5,
        "p_first": [[0.5] * 6], "tickets": [[{"combo": "1-2-3", "stake": 2000}]], "manshu_prob": [0.3],
        "longshot_tickets": [[]], "tri_probs": [{"1-2-3": 0.2}], "hit_probability": [0.5],
    })
    c.attrs["manshu_version"] = "m"
    rows = pre_picks.manshu_rows(pre_picks.select_manshu(c).pipe(lambda m: (setattr(m, "attrs", dict(c.attrs)), m)[1]))
    assert rows[0]["tri_probs"] == {"1-2-3": 0.2}

    calls = []

    def sb(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "POST" and path == "manshu_chance_picks" and "tri_probs" in kw["json"][0]:
            raise RuntimeError("400 column tri_probs does not exist")

        class R:
            def json(s):
                return []
        return R()
    orig = pre_picks._sb
    pre_picks._sb = sb
    try:
        n, err = pre_picks.save_manshu("20261018", c)
    finally:
        pre_picks._sb = orig
    posts = [j for m, p, j in calls if m == "POST" and p == "manshu_chance_picks"]
    assert n == 1 and err is None and len(posts) == 2 and "tri_probs" not in posts[1][0]


def test_zero_stake_candidate_is_not_a_hit():
    """資金配分で0円になった候補が来ても的中にしない（画面に出していない買い目）。"""
    tickets = [{"combo": "1-2-4", "stake": 1500}, {"combo": "1-4-3", "stake": 0}]
    assert pre_picks.ticket_result(tickets, [("1-4-3", 1650)]) == (False, 1500, 0)
    assert pre_picks.ticket_result(tickets, [("1-2-4", 1000)]) == (True, 1500, 15000)


def test_morning_aim_tickets_saved_with_rows():
    """朝の選定で、10レース分のオッズから中穴〜万舟狙いの買い目を作って一緒に保存する。"""
    probs = {"2-1-3": 0.06, "3-1-2": 0.05, "5-1-2": 0.02, "1-2-3": 0.3}
    odds = {"2-1-3": 31.0, "3-1-2": 45.0, "5-1-2": 150.0, "1-2-3": 3.0}
    rows = [{"race_key": "20261019_01_1", "tri_probs": probs}, {"race_key": "20261019_01_2", "tri_probs": probs},
            {"race_key": "20261019_01_3", "tri_probs": None}]
    fetched = []

    def fetch(rk):
        fetched.append(rk)
        if rk.endswith("_2"):
            return {}  # まだオッズが出ていない
        return odds
    n = pre_picks.add_morning_aim(rows, fetch=fetch)
    assert n == 1 and fetched == ["20261019_01_1", "20261019_01_2"]  # 確率の無いレースは取りに行かない
    assert [t["combo"] for t in rows[0]["aim_tickets"]] == ["2-1-3", "3-1-2", "5-1-2"]
    assert rows[0]["aim_source"] == "morning" and rows[0]["aim_odds_at"]
    # まとめて保存できるよう、全部の行に同じ列がそろう
    assert all(set(r) >= {"aim_tickets", "aim_method", "aim_source", "aim_odds_at", "aim_saved_at"} for r in rows)
    assert rows[1]["aim_tickets"] is None and rows[2]["aim_tickets"] is None


def test_jst_hhmm():
    import manshu_view

    assert manshu_view.jst_hhmm("2026-10-18T00:30:12+00:00") == "09:30"  # データベースは世界標準時で返す
    assert manshu_view.jst_hhmm("2026-10-18T10:12:00+09:00") == "10:12"
    assert manshu_view.jst_hhmm(None) == "" and manshu_view.jst_hhmm("x") == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
