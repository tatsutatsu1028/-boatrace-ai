"""
一覧（💥 万舟チャンス・🌅 展示前厳選10）の「📝 note記事」「🧵 スレッズ投稿」と、予想タブの同じ欄が
同じ中身になるか。使う予想の選び方（展示後 → 朝）と、二重投稿の確認の判定も確かめる。

  python -m pytest tests/test_race_posts.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import note_article as na  # noqa: E402
import race_posts as rp  # noqa: E402
from result_tracker import build_snapshot_payload_json  # noqa: E402

RK = "20261023_01_5"
D = date(2026, 10, 23)


def _final():
    return pd.DataFrame({
        "lane": range(1, 7), "racer_name": ["山田", "佐藤", "鈴木", "高橋", "田中", "伊藤"],
        "p_first": [.18, .22, .2, .15, .14, .11], "p_second": [.17] * 6, "p_third": [.16] * 6,
        "model_version": ["ml-chain-v3"] * 6})


def _tickets():
    return pd.DataFrame([
        {"combo": "2-3-1", "group": "本線", "prob": .05, "stake": 600.0},
        {"combo": "2-3-4", "group": "本線", "prob": .04, "stake": 500.0},
        {"combo": "3-2-1", "group": "抑え", "prob": .03, "stake": 400.0},
        {"combo": "4-2-3", "group": "穴", "prob": .01, "stake": 0.0},
    ])


def _work():
    return pd.DataFrame({"lane": range(1, 7), "racer_name": ["山田", "佐藤", "鈴木", "高橋", "田中", "伊藤"],
                         "avg_st": [.15] * 6, "exhibition_st": [.12] * 6, "wind_speed": [6.0] * 6,
                         "wave_height": [5.0] * 6, "unused_live_only": [1] * 6})


def _snapshot(pre_exhibition=False, collector="auto_post"):
    text, h = build_snapshot_payload_json(_final(), _tickets(), race_features=_work(),
                                          pre_exhibition=pre_exhibition)
    return {"race_key": RK, "payload_json": text, "payload_hash": h, "pre_exhibition": pre_exhibition,
            "collector_name": collector, "saved_at": "2026-10-23T05:41:00+00:00"}


MORNING = {"race_key": RK, "venue": "桐生", "race_no": 5, "deadline": "15:12", "favorite_lane": 1,
           "favorite_name": "山田", "p_first": [.3, .2, .15, .15, .1, .1],
           "tickets": [{"combo": "1-2-3", "group": "本線", "prob": .08, "stake": 800},
                       {"combo": "1-3-2", "group": "抑え", "prob": .05, "stake": 0}]}
MARK = {"race_key": RK, "deadline": "15:12", "aim_source": "auto", "aim_odds_at": "2026-10-23T05:42:00+00:00",
        "aim_tickets": [{"combo": "2-3-4", "tier": "中穴", "odds": 45.0, "prob": .01},
                        {"combo": "4-5-6", "tier": "万舟", "odds": 180.0, "prob": .004}]}


# ---------------------------------------------------------------
# 使う予想
# ---------------------------------------------------------------
def test_post_exhibition_snapshot_is_used_with_label():
    for who, label in (("auto_post", "展示後の自動予想"), ("someone", "展示後の手動予想"),
                       ("auto_random", "展示後の自動固定")):
        p = rp.choose_prediction(_snapshot(collector=who), MORNING)
        assert p["kind"] == "post" and p["label"] == label
        assert p["work"] is not None and "wind_speed" in p["work"].columns
        assert p["tickets"]["stake"].dtype.kind == "i"


def test_morning_is_used_without_post_exhibition_prediction():
    for snap in (None, _snapshot(pre_exhibition=True)):
        p = rp.choose_prediction(snap, MORNING)
        assert p["kind"] == "morning" and p["label"] == rp.MORNING_LABEL and p["work"] is None
        assert list(p["final"]["p_first"]) == MORNING["p_first"]
        assert p["final"].set_index("lane").at[1, "racer_name"] == "山田"
        assert list(p["tickets"]["combo"]) == ["1-2-3", "1-3-2"]
    assert rp.choose_prediction(None, {"race_key": RK}) is None


def test_list_and_predict_tab_restore_the_same_frames():
    """予想タブの「保存された最新の予想」（画面の予想と同じならそのまま）と、一覧で読み直した予想が同じ記事・投稿になる。"""
    snap = _snapshot()
    shown_final, shown_tickets = _final(), _tickets()
    shown_tickets["stake"] = shown_tickets["stake"].astype(int)
    listed = rp.choose_prediction(snap, MORNING)
    restored_final, restored_tickets, _ = rp.frames_from_snapshot(snap)
    for final, tickets in ((shown_final, shown_tickets), (restored_final, restored_tickets)):
        assert na.tickets_list(tickets) == na.tickets_list(listed["tickets"])
        assert na.p_first_map(final) == na.p_first_map(listed["final"])
        assert na.template_article(na.tickets_list(tickets)) == na.template_article(na.tickets_list(listed["tickets"]))
        for style in ("normal", "aim"):
            assert rp.threads_text(D, "桐生", 5, final, tickets, "15:12", None, style) == \
                rp.threads_text(D, "桐生", 5, listed["final"], listed["tickets"], "15:12", None, style)


def test_style_follows_note_tickets():
    assert rp.style_for("aim") == "aim"
    assert rp.style_for("model") == rp.style_for("avg") == rp.style_for(None) == "normal"


# ---------------------------------------------------------------
# 二重投稿の防止
# ---------------------------------------------------------------
AUTO = {"race_date": "2026-10-23", "status": "posted", "posted_at": "2026-10-23T00:00:40+00:00",
        "race_keys": ["20261023_02_1", RK, "20261023_03_2"],
        "posts": [{"race_key": "20261023_02_1", "post_id": "a"}, {"race_key": RK, "post_id": "b"},
                  {"race_key": "20261023_03_2", "post_id": "c"}]}


def test_status_for_note_threads_and_morning_auto_post():
    notes = [{"race_key": RK, "created_at": "2026-10-23T05:50:00+00:00"}]
    threads = [{"race_key": RK, "posted_at": "2026-10-23T06:01:00+00:00"},
               {"race_key": "20261023_09_9", "posted_at": "2026-10-23T06:02:00+00:00"}]
    s = rp.posted_status(RK, notes, threads, AUTO)
    assert len(s["threads"]) == 1 and s["auto"]["posted"] and s["auto"]["rank"] == 2
    lines = rp.status_lines(s)
    assert "確定済み（14:50）" in lines[0]
    assert "投稿済み（15:01）" in lines[1]
    assert "毎朝9:00の自動投稿" in lines[2] and "09:00" in lines[2]
    assert rp.needs_confirm(s)


def test_no_confirm_when_not_posted_yet():
    s = rp.posted_status(RK, [], [], None)
    assert rp.status_lines(s) == [] and not rp.needs_confirm(s)
    # 朝の自動投稿に選ばれたが投稿できなかったレースは、そう書くだけ（確認はいらない）
    failed = {**AUTO, "status": "failed", "posts": [{**p, "post_id": None} for p in AUTO["posts"]]}
    s = rp.posted_status(RK, [], [], failed)
    assert not rp.needs_confirm(s) and "失敗" in rp.status_lines(s)[0]
    assert rp.posted_status(RK, [], [], {**AUTO, "race_keys": [], "posts": []})["auto"] is None


def test_threads_record_row():
    row = rp.threads_record(RK, 123, "本文", "aim", "aim", "展示後の自動予想", "list")
    assert row == {"race_key": RK, "race_date": "2026-10-23", "post_id": "123", "text": "本文", "style": "aim",
                   "ticket_source": "aim", "prediction": "展示後の自動予想", "screen": "list"}


# ---------------------------------------------------------------
# 画面: 予想タブの欄と一覧の欄を同じ実行の中で出し、中身が同じか（ボタンの key もぶつからないか）
# ---------------------------------------------------------------
def _app(snapshot_text, mark, list_style=None):
    import json as _json
    from datetime import date as _date

    import pandas as _pd

    import claude_tab
    import note_view
    import race_posts
    import threads_view

    note_view.load_publication = lambda rk: None
    note_view._saved_claude_section = lambda rk: None
    note_view._manshu_mark = lambda rk, d: mark
    note_view._race_flags = lambda rk, d: (True, False)
    claude_tab.bundle = lambda rk: None
    threads_view.race_status = lambda rk, notes=(): race_posts.posted_status(rk)

    snap = _json.loads(snapshot_text)
    shown_final = _pd.DataFrame(_json.loads(snap["payload_json"])["final"])
    shown_tickets = _pd.DataFrame(_json.loads(snap["payload_json"])["tickets"])
    shown_tickets["stake"] = shown_tickets["stake"].astype(int)
    listed = race_posts.choose_prediction(snap, {})
    d = _date(2026, 10, 23)
    cfg = {"user_id": "u", "access_token": "t"}
    # 予想タブ（画面の予想）
    pub = note_view.render(snap["race_key"], d, "桐生", 5, "15:12", shown_final, shown_tickets, None)
    threads_view.render(snap["race_key"], d, "桐生", 5, "15:12", shown_final, shown_tickets, pub, cfg,
                        is_manshu=True)
    # 一覧（保存された展示後の予想・最初は中穴〜万舟狙い）
    pub = note_view.render(snap["race_key"], d, "桐生", 5, "15:12", listed["final"], listed["tickets"],
                           listed["work"], key_prefix="list_pub_", default_source="aim")
    threads_view.render(snap["race_key"], d, "桐生", 5, "15:12", listed["final"], listed["tickets"], pub, cfg,
                        is_manshu=True, key_prefix="list_pub_", screen="list", default_style=list_style)


def _run(mark=None):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_app, args=(json.dumps(_snapshot()), mark), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _areas(at):
    return {a.label + ("（一覧）" if a.key.startswith("list_pub_") else ""): a.value for a in at.text_area}


def test_note_and_threads_panels_match_between_predict_tab_and_list():
    at = _run(None)
    areas = _areas(at)
    for label in ("note本文（そのままコピー用）", "無料部分（買い目なし）", "有料部分（買い目・詳しい展開）",
                  "投稿内容（送信前に編集できます）"):
        assert areas[label] == areas[label + "（一覧）"], label
    assert "【本線買い目】" in areas["note本文（そのままコピー用）"]
    assert "桐生 5R" in areas["投稿内容（送信前に編集できます）"]
    titles = [t.value for t in at.text_input]
    assert len(titles) == 2 and titles[0] == titles[1]


def test_aim_tickets_selected_first_in_list_and_same_article():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_app, args=(json.dumps(_snapshot()), MARK), default_timeout=30)
    # 一覧の欄は最初から「中穴〜万舟狙い」（予想タブは今までどおりモデルのみ）
    at.run()
    assert at.radio(key=f"note_source_{RK}").value == "model"
    assert at.radio(key=f"list_pub_note_source_{RK}").value == "aim"
    at.radio(key=f"note_source_{RK}").set_value("aim")
    at.run()
    areas = _areas(at)
    assert areas["note本文（そのままコピー用）"] == areas["note本文（そのままコピー用）（一覧）"]
    assert "2-3-4" in areas["note本文（そのままコピー用）"] and "4-5-6" in areas["note本文（そのままコピー用）"]


def test_threads_aim_text_same_in_list_and_predict_tab():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_app, args=(json.dumps(_snapshot()), MARK, "aim"), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert at.radio(key=f"threads_style_{RK}").value == "normal"
    assert at.radio(key=f"list_pub_threads_style_{RK}").value == "aim"
    at.radio(key=f"threads_style_{RK}").set_value("aim")
    at.run()
    areas = _areas(at)
    text = areas["投稿内容（送信前に編集できます）"]
    assert text == areas["投稿内容（送信前に編集できます）（一覧）"] and "万舟チャンス" in text


def test_list_default_source_is_aim_for_manshu_chance():
    def app(snapshot_text, mark):
        import json as _json
        from datetime import date as _date

        import note_view
        import race_posts

        note_view.load_publication = lambda rk: None
        note_view._saved_claude_section = lambda rk: None
        note_view._manshu_mark = lambda rk, d: mark
        note_view._race_flags = lambda rk, d: (True, False)
        snap = _json.loads(snapshot_text)
        listed = race_posts.choose_prediction(snap, {})
        note_view.render(snap["race_key"], _date(2026, 10, 23), "桐生", 5, "15:12", listed["final"],
                         listed["tickets"], listed["work"], key_prefix="list_pub_", default_source="aim")

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(app, args=(json.dumps(_snapshot()), MARK), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert at.radio(key=f"list_pub_note_source_{RK}").value == "aim"
    # 中穴〜万舟狙いの買い目が無ければ、モデルのみ予想にする
    at = AppTest.from_function(app, args=(json.dumps(_snapshot()), {**MARK, "aim_tickets": []}), default_timeout=30)
    at.run()
    assert at.radio(key=f"list_pub_note_source_{RK}").value == "model"


def test_threads_repost_needs_confirm():
    def app(snapshot_text, auto):
        import json as _json
        from datetime import date as _date

        import race_posts
        import threads_view

        threads_view.race_status = lambda rk, notes=(): race_posts.posted_status(rk, (), (), auto)
        snap = _json.loads(snapshot_text)
        listed = race_posts.choose_prediction(snap, {})
        threads_view.render(snap["race_key"], _date(2026, 10, 23), "桐生", 5, "15:12", listed["final"],
                            listed["tickets"], None, {"user_id": "u", "access_token": "t"}, key_prefix="list_pub_",
                            screen="list")

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(app, args=(json.dumps(_snapshot()), AUTO), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert any("毎朝9:00の自動投稿" in w.value for w in at.warning)
    assert at.button(key=f"list_pub_post_threads_{RK}").disabled
    at.checkbox(key=f"list_pub_threads_again_{RK}").check()
    at.run()
    assert not at.button(key=f"list_pub_post_threads_{RK}").disabled
    # 投稿していないレースは確認なしで押せる
    at = AppTest.from_function(app, args=(json.dumps(_snapshot()), None), default_timeout=30)
    at.run()
    assert not at.button(key=f"list_pub_post_threads_{RK}").disabled and not at.checkbox


def test_list_panel_opens_note_and_threads_with_morning_prediction():
    def app(row, mark):
        from datetime import date as _date

        import claude_tab
        import manshu_view
        import note_view
        import publish_panel
        import race_posts
        import threads_view

        note_view.load_publication = lambda rk: None
        note_view._saved_claude_section = lambda rk: None
        note_view._manshu_mark = lambda rk, d: mark
        note_view._race_flags = lambda rk, d: (True, False)
        note_view.load_day_publications = lambda iso: []
        claude_tab.bundle = lambda rk: None
        manshu_view.marks_by_key = lambda d: {mark["race_key"]: mark}
        threads_view.race_status = lambda rk, notes=(): race_posts.posted_status(rk)
        threads_view.load_config = lambda: {"user_id": "u", "access_token": "t"}
        publish_panel._snapshot = lambda rk: None
        publish_panel.render(_date(2026, 10, 23), row, "manshu_list")

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(app, args=(MORNING, MARK), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert not at.text_area  # 押すまでは開かない（読み込まない）
    at.button(key=f"manshu_list_pub_note_{RK}_btn").click().run()
    assert not at.exception, at.exception
    assert any("朝の予想（展示前）" in i.value for i in at.info)
    assert at.radio(key=f"manshu_list_pub_note_source_{RK}").value == "aim"
    assert any("展示後の自動予想で" in c.value for c in at.caption)  # 中穴〜万舟狙いを作った時刻
    at.button(key=f"manshu_list_pub_threads_{RK}_btn").click().run()
    assert not at.exception, at.exception
    assert at.radio(key=f"manshu_list_pub_threads_style_{RK}").value == "aim"
    assert "万舟チャンス" in at.text_area(key=f"manshu_list_pub_threads_text_v2_{RK}_aim").value
