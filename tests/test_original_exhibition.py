"""
オリジナル展示の自動取得（original_exhibition_fetcher.py）の、通信・Supabase が無くても確かめられる部分。

  python -m pytest tests/test_original_exhibition.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import original_exhibition_fetcher as oe  # noqa: E402

JST = oe.JST
RK = "20261110_05_12"


def _page(rows, head=("一周", "まわり足", "直線"), rowspan=False):
    """各場の表と同じ作り（枠番のセルに waku、最後の3セルが見出しの順）の小さなページ。"""
    th = "".join(f"<th>{h}</th>" for h in head)
    body = ""
    for lane, a, b, c in rows:
        rs = " rowspan='2'" if rowspan else ""
        body += (f"<tr><td class='col1 waku tei_color{lane} '{rs}>{lane}</td><td class='col2'>選手</td>"
                 f"<td class='col3'>52.0</td><td class='col5'{rs}>0.0</td><td class='col6'{rs}>6.70</td>"
                 f"<td class=\"col7\"{rs}>{a}</td>\n<td class=\"col8\"{rs}>{b}</td>\n<td class=\"col9\"{rs}>{c}</td></tr>")
        if rowspan:
            body += "<tr><td class='col3'>0.5</td></tr>"
    return (f"<ul><li>オリジナル展示データ</li></ul><table><thead><tr><th>枠</th><th>体重</th><th>展示</th>"
            f"{th}</tr></thead><tbody>{body}</tbody></table>")


ROWS = [(1, "36.63", "5.47", "6.87"), (2, "36.90", "5.63", "7.00"), (3, "36.17", "5.73", "6.83"),
        (4, "37.73", "5.73", "6.87"), (5, "37.44", "5.66", "6.88"), (6, "37.23", "-", "6.90")]


def test_parse_reads_six_lanes():
    for rowspan in (False, True):
        st, rows, why = oe.parse(_page(ROWS, rowspan=rowspan))
        assert st == "ok" and why is None
        assert rows[1] == {"original_lap": 36.63, "original_turn": 5.47, "original_straight": 6.87}
        assert rows[6]["original_turn"] is None   # 「-」は計測なし


def test_parse_follows_header_order():
    swapped = [(lane, c, a, b) for lane, a, b, c in ROWS]   # 直線・一周・まわり足 の順
    st, rows, _ = oe.parse(_page(swapped, head=("直線", "一周", "まわり足")))
    assert st == "ok" and rows[1] == {"original_lap": 36.63, "original_turn": 5.47, "original_straight": 6.87}


def test_parse_unpublished_and_format():
    assert oe.parse("<p>ただいまデータはございません。</p>")[0] == "unpublished"
    assert oe.parse("<p>次節開催までしばらくお待ちください</p>")[0] == "unpublished"
    empty = [(lane, "-", "-", "-") for lane in range(1, 7)]
    assert oe.parse(_page(empty))[0] == "unpublished"
    assert oe.parse(_page(ROWS, head=("半周", "まわり足", "直線")))[0] == "format"
    assert oe.parse("<p>表がない</p>")[0] == "format"
    assert oe.parse(_page(ROWS[:5]))[0] == "format"
    bad = [(1, "52.0", "5.47", "6.87")] + ROWS[1:]   # 一周の範囲外（体重などのずれ）
    assert oe.parse(_page(bad))[0] == "format"
    assert oe.parse("")[0] == "format"


def test_source_url_and_unsupported():
    assert oe.source_url("05", RK) == ("https://www.boatrace-tamagawa.com/modules/yosou/oriten.php"
                                       "?day=20261110&race=12&jo=05&if=1")
    assert "kind=2" in oe.source_url("8", "20261110_08_3") and "race=3" in oe.source_url("8", "20261110_08_3")
    assert oe.source_url("03", "20261110_03_1") is None
    assert oe.source_url("04", "20261110_04_3").endswith("/yoso0503.htm")      # 2桁のレース番号
    assert oe.source_url("12", "20261110_12_11").endswith("/st0211.htm")
    assert oe.UNSUPPORTED["07"].startswith("対象外") and oe.UNSUPPORTED["24"].startswith("対象外")
    assert oe.source_url("16", "20261112_16_7").endswith("/kyogi/16/pc/st0207.htm")


def _row_html(lane, cells, lane_cell=None):
    lane_cell = lane_cell or f"<td class='waku0{lane}'>{lane}</td>"
    return "<tr>" + lane_cell + "<td>選手</td>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def test_parse_trailing_columns_and_two_labels():
    head = "<table><tr><th>枠</th><th>一周</th><th>まわり足</th><th>直線</th><th>展示</th><th>体重</th><th>部品</th></tr>"
    rows = "".join(_row_html(l, ["36.6%d" % l, "5.50", "7.37", "6.79", "52.1", "リング×4"]) for l in range(1, 7))
    st, out, _ = oe.parse(head + rows + "</table>", trailing=3)        # 平和島: 後ろに3列
    assert st == "ok" and out[1] == {"original_lap": 36.61, "original_turn": 5.5, "original_straight": 7.37}
    assert oe.parse(head + rows + "</table>")[0] == "format"           # 列の数を間違えると形式違い
    head2 = "<table><tr><th>枠</th><th>展示</th><th>一周</th><th>まわり足</th></tr>"
    rows2 = "".join(_row_html(l, ["6.70", "37.10", "5.40"]) for l in range(1, 7))
    st, out, _ = oe.parse(head2 + rows2 + "</table>", labels=("一周", "まわり足"))   # 住之江: 直線なし
    assert st == "ok" and out[2] == {"original_lap": 37.1, "original_turn": 5.4, "original_straight": None}


def test_parse_wakuban_cell_and_no_lap():
    head = "<table><tbody><tr><td>枠</td><td>展示</td><td>一周</td><td>まわり足</td><td>直線</td></tr>"
    rows = "".join(_row_html(l, ["6.92", "<i>--.--</i>", "5.97", "7.26"],
                             lane_cell=f"<td rowspan='2'><p class='wakuban'>{l}</p></td>") for l in range(1, 7))
    st, out, _ = oe.parse(head + rows + "</tbody></table>")             # 宮島: 周回短縮で一周なし
    assert st == "ok" and out[1] == {"original_lap": None, "original_turn": 5.97, "original_straight": 7.26}


def test_miyajima_race_mark():
    import re
    mark = oe._fill(oe.VENUES["17"]["race_mark"], "20261110_17_1")
    tabs = "1R 2R 3R 4R 5R 6R 7R 8R 9R 10R 11R 12R"
    assert re.search(mark, tabs + " 1 R ファースト")
    assert not re.search(mark, tabs + " 11 R 一般")
    assert not re.search(mark, tabs + " 5 R 予選")


def _t(jcd="05", deadline="15:30", sources=("manshu",), **kw):
    return {"race_key": f"20261110_{jcd}_12", "race_date": "2026-11-10", "jcd": jcd, "race_no": 12,
            "deadline": deadline, "sources": list(sources), **kw}


def _at(hhmm):
    return datetime.strptime(f"2026-11-10 {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)


def test_needs_fetch_once_then_one_retry():
    now = _at("15:15")
    assert not oe.needs_fetch(_t(sources=()), None, "done", now)                # 万舟チャンス・厳選10だけ
    assert oe.needs_fetch(_t(sources=("pre",)), None, "done", now)               # 厳選10だけのレースも取る
    assert not oe.needs_fetch(_t(cancelled=True), None, "done", now)
    assert not oe.needs_fetch(_t(), None, "waiting", now)                        # 展示後予想の前は取らない
    assert oe.needs_fetch(_t(), None, "done", now) and oe.needs_fetch(_t(), None, "already", now)
    prev = {"status": "unpublished", "attempts": 1, "last_tried_at": _at("15:15").isoformat()}
    assert not oe.needs_fetch(_t(), prev, "done", _at("15:17"))                  # まだ数分たっていない
    assert oe.needs_fetch(_t(), prev, "done", _at("15:20"))                      # 1回だけ取り直す
    assert not oe.needs_fetch(_t(), {**prev, "attempts": 2}, "done", _at("15:30"))
    assert not oe.needs_fetch(_t(), {**prev, "status": "ok"}, "done", _at("15:30"))
    assert not oe.needs_fetch(_t(), {**prev, "status": "format"}, "done", _at("15:30"))
    assert not oe.needs_fetch(_t(), prev, "done", _at("16:40"))                  # 締切から時間がたちすぎ


class FakeSB:
    def __init__(self, log_rows=()):
        self.calls = []
        self.log_rows = list(log_rows)

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        rows = self.log_rows if method == "GET" else None

        class R:
            def json(self_inner):
                return rows
        return R()


def test_record_ok_writes_lanes_and_log(monkeypatch):
    monkeypatch.setattr(oe, "fetch", lambda jcd, rk: oe.parse(_page(ROWS)))
    sb = FakeSB()
    assert oe.record(sb, _t(), None) == "ok"
    (m1, p1, k1), (m2, p2, k2) = sb.calls
    assert p1.startswith(oe.TABLE + "?") and len(k1["json"]) == 6
    assert k1["json"][0]["lane"] == 1 and k1["json"][0]["source_url"].startswith("https://www.boatrace-tamagawa")
    assert p2.startswith(oe.LOG_TABLE) and k2["json"]["status"] == "ok" and k2["json"]["attempts"] == 1


def test_record_unsupported_does_not_fetch(monkeypatch):
    def boom(jcd, rk):
        raise AssertionError("通信しない")

    monkeypatch.setattr(oe, "fetch", boom)
    sb = FakeSB()
    assert oe.record(sb, _t(jcd="03"), None) == "unsupported"
    (_, path, kw), = sb.calls
    assert kw["json"]["status"] == "unsupported" and kw["json"]["attempts"] == 0 and "公開していない" in kw["json"]["reason"]


def test_record_unpublished_notes_retry(monkeypatch):
    monkeypatch.setattr(oe, "fetch", lambda jcd, rk: ("unpublished", None, "未公開（値が入っていない）"))
    sb = FakeSB()
    assert oe.record(sb, _t(), None) == "unpublished"
    assert "取り直す" in sb.calls[-1][2]["json"]["reason"]
    sb = FakeSB()
    oe.record(sb, _t(), {"attempts": 1, "status": "unpublished"})
    assert sb.calls[-1][2]["json"]["attempts"] == 2 and "取り直す" not in sb.calls[-1][2]["json"]["reason"]


def test_run_for_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    assert oe.run_for(boom, [_t()], {}) == {}                          # 記録が読めない
    monkeypatch.setattr(oe, "record", boom)
    sb = FakeSB()
    assert oe.run_for(sb, [_t()], {_t()["race_key"]: {"status": "done"}}) == {"error": 1}
    assert oe.run_for(sb, [_t(sources=("pre",))], {}) == {}


def test_auto_post_hook_swallows_errors(monkeypatch):
    import auto_post_predict as ap

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ap, "load_log", boom)
    assert ap.record_original_exhibition("20261110", [_t()]) == {}
    assert ap.record_original_exhibition("20261110", [_t(sources=("pre",))]) == {}


def test_before_deadline():
    t = _t(deadline="15:30")
    assert oe.before_deadline(t, _at("15:17")) is True
    assert oe.before_deadline(t, _at("15:31")) is False
    assert oe.before_deadline(_t(deadline=None), _at("15:17")) is None


def test_record_writes_deadline_flag(monkeypatch):
    monkeypatch.setattr(oe, "fetch", lambda jcd, rk: oe.parse(_page(ROWS)))
    sb = FakeSB()
    oe.record(sb, {**_t(deadline="23:59"), "race_date": "2099-01-01"}, None)   # 締切はまだ先
    row = sb.calls[-1][2]["json"]
    assert row["deadline"] == "23:59" and row["before_deadline"] is True
    # 取れなかったレースは判定しない（null）
    monkeypatch.setattr(oe, "fetch", lambda jcd, rk: ("unpublished", None, "未公開（値が入っていない）"))
    sb = FakeSB()
    oe.record(sb, _t(deadline="23:59"), None)
    assert sb.calls[-1][2]["json"]["before_deadline"] is None


def _xml(vals):
    recs = "".join(f"<record><teiban>{l}</teiban><name>選手</name><ttime>6.73</ttime><str>{c}</str><cnr>{b}</cnr>"
                   f"<rnd>{a}</rnd></record>" for l, a, b, c in vals)
    return f'<?xml version="1.0" encoding="utf-8" ?><table>{recs}</table>'


def test_parse_xml_toda():
    st, out, _ = oe.parse_xml(_xml(ROWS))
    assert st == "ok" and out[1] == {"original_lap": 36.63, "original_turn": 5.47, "original_straight": 6.87}
    assert out[6]["original_turn"] is None
    assert oe.parse_xml(_xml([(l, "", "", "") for l in range(1, 7)]))[0] == "unpublished"
    assert oe.parse_xml(_xml(ROWS[:5]))[0] == "format"
    assert oe.parse_xml("<table><record>")[0] == "format"
    assert oe.parse_xml("")[0] == "unpublished"
    assert oe.source_url("02", "20261112_02_1").endswith("/xml/kaisai/20261112/race_table_original_01.xml")


def test_fetch_xml_404_is_unpublished(monkeypatch):
    import polite_http

    class R:
        status_code = 404
        content = b""

    monkeypatch.setattr(polite_http, "get", lambda url, timeout=15, binary=False: R())
    assert oe.fetch("02", "20261112_02_1")[0] == "unpublished"
    assert oe.fetch("05", "20261112_05_1")[0] == "error"                 # HTML の場の 404 は取得失敗のまま


def test_target_kind():
    assert oe.target_kind(_t(sources=("manshu",))) == "manshu"
    assert oe.target_kind(_t(sources=("pre",))) == "pick"
    assert oe.target_kind(_t(sources=("pre", "manshu"))) == "both"
    assert oe.target_kind(_t(sources=())) is None


def test_record_writes_target_kind(monkeypatch):
    monkeypatch.setattr(oe, "fetch", lambda jcd, rk: oe.parse(_page(ROWS)))
    for src, kind in ((("pre",), "pick"), (("pre", "manshu"), "both"), (("manshu",), "manshu")):
        sb = FakeSB()
        oe.record(sb, _t(sources=src), None)
        assert sb.calls[-1][2]["json"]["target_kind"] == kind
    sb = FakeSB()
    oe.record(sb, _t(jcd="03", sources=("pre",)), None)                      # 対象外の場も今と同じ（通信しない）
    assert sb.calls[-1][2]["json"]["status"] == "unsupported" and sb.calls[-1][2]["json"]["target_kind"] == "pick"


def test_run_for_both_once_and_cap(monkeypatch):
    """両方に入っているレースは1件の対象なので1回だけ。1回の起動は MAX_PER_RUN まで、締切前を先に。"""
    done = []
    monkeypatch.setattr(oe, "record", lambda sb, t, p, now=None: done.append(t["race_key"]) or "ok")
    now = _at("15:00")
    ts = [{**_t(deadline=f"{h}:00", sources=("pre", "manshu") if i == 0 else ("pre",)),
           "race_key": f"20261110_05_{i + 1}"} for i, h in enumerate([16, 11, 12, 13, 14, 17, 18, 10])]
    post = {t["race_key"]: {"status": "done"} for t in ts}
    assert oe.run_for(FakeSB(), ts, post, now) == {"ok": oe.MAX_PER_RUN}
    assert len(done) == len(set(done)) == oe.MAX_PER_RUN
    # 締切前（16:00・17:00・18:00）が締切の早い順に先、その後は締切後で締切から間もない順（14:00・13:00・12:00）
    assert done == ["20261110_05_1", "20261110_05_6", "20261110_05_7",
                    "20261110_05_5", "20261110_05_4", "20261110_05_3"]


def test_run_for_cap_counts_only_fetching_races(monkeypatch):
    """対象外の場（通信しない）は上限に数えず、通信する場が6レースあってもすべて記録する。"""
    done = []
    monkeypatch.setattr(oe, "record", lambda sb, t, p, now=None: done.append(t["race_key"]) or "ok")
    now = _at("15:00")
    online = [{**_t(deadline="16:00", sources=("pre",)), "race_key": f"20261110_05_{i}"} for i in range(1, 9)]
    offline = [{**_t(jcd="03", deadline="16:00", sources=("pre",)), "race_key": f"20261110_03_{i}"} for i in range(1, 4)]
    post = {t["race_key"]: {"status": "done"} for t in online + offline}
    oe.run_for(FakeSB(), online + offline, post, now)
    assert sum(k.startswith("20261110_03_") for k in done) == 3                  # 対象外はすべて
    assert sum(k.startswith("20261110_05_") for k in done) == oe.MAX_PER_RUN    # 通信する場は上限まで
