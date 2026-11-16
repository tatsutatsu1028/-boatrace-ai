"""
オリジナル展示の自動取得（original_exhibition_fetcher.py）の、通信・Supabase が無くても確かめられる部分。

  python -m pytest tests/test_original_exhibition.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re  # noqa: E402

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


# ---------------------------------------------------------------
# 尼崎・若松・唐津・徳山・丸亀の追加、津の「表示するデータがありません」、未確認の場を試す
# ---------------------------------------------------------------
def test_new_venues_source_url():
    assert oe.source_url("13", "20261116_13_6") == ("https://www.boatrace-amagasaki.jp/modules/yosou/group-cyokuzen.php"
                                                    "?day=20261116&race=6&kind=2&if=1")
    assert oe.source_url("20", "20261116_20_1").startswith("https://www.wmb.jp/modules/yosou/group-cyokuzen.php")
    assert oe.source_url("23", "20261116_23_11").endswith("day=20261116&race=11&kind=2&if=1")
    assert oe.source_url("18", "20261116_18_3").endswith("/modules/yosou/tenji.php?day=20261116&race=3&if=1")
    assert oe.source_url("15", "20261116_15_7").endswith("/asp/kyogi/15/pc/yoso0507.htm")
    for jcd in ("02", "13", "15", "16", "18", "20", "23"):
        assert jcd in oe.VENUES and jcd not in oe.UNSUPPORTED
    assert oe.UNSUPPORTED["24"].startswith("対象外（オリジナル展示を公開していない")


def test_parse_amagasaki_turn_range():
    rows = [(l, "36.4%d" % l, "11.2%d" % l, "") for l in range(1, 7)]
    page = _page(rows).replace("<th>直線</th>", "")
    page = page.replace("<td class=\"col9\"></td>", "")
    assert oe.parse(page, ("一周", "まわり足"))[0] == "format"              # 既定の範囲（〜10秒）では外れる
    st, out, _ = oe.parse(page, ("一周", "まわり足"), ranges=oe.VENUES["13"]["ranges"])
    assert st == "ok" and out[1] == {"original_lap": 36.41, "original_turn": 11.21, "original_straight": None}


def test_parse_tokuyama_lane_in_second_cell_and_br_label():
    head = ("<table><tr><th>気<br>配</th><th>枠</th><th>展示</th><th>一周</th><th>まわ<br>り足</th>"
            "<th>レース</th><th>コース</th><th>ST</th><th>着<br>順</th></tr>")
    rows = "".join(f"<tr><td class='col1 '>A</td><td class='col2 tei_color{l} waku'>{l}</td><td>選手</td>"
                   f"<td>6.8{l}</td><td>36.5{l}</td><td>11.2{l}</td><td></td><td></td><td></td><td></td></tr>"
                   "<tr><td class='col-buhin' colspan='5'><ul><li>ギヤ</li></ul></td></tr>" for l in range(1, 7))
    v = oe.VENUES["18"]
    st, out, _ = oe.parse(head + rows + "</table>", v["labels"], v["trailing"], v["ranges"])
    assert st == "ok" and out[3] == {"original_lap": 36.53, "original_turn": 11.23, "original_straight": None}


def test_parse_marugame_plain_lane():
    head = "<table><tr><th>枠</th><th>展示</th><th>一周</th><th>まわり足</th><th>直線</th></tr>"
    rows = "".join(f"<tbody><tr><td rowspan='2'>{l}</td><td rowspan='2'><div>選手</div></td><td>52.0</td>"
                   f"<td rowspan='2'>6.7{l}</td><td>36.2{l}</td><td>5.6{l}</td><td>5.5{l}</td></tr>"
                   "<tr><td class='small'>&nbsp;</td></tr></tbody>" for l in range(1, 7))
    assert oe.parse(head + rows + "</table>")[0] == "format"                  # 枠の印が無いと読めない
    st, out, _ = oe.parse(head + rows + "</table>", plain_lane=True)
    assert st == "ok" and out[6] == {"original_lap": 36.26, "original_turn": 5.66, "original_straight": 5.56}
    empty = "".join(f"<tbody><tr><td rowspan='2'>{l}</td><td rowspan='2'><div>選手</div></td><td>52.0</td>"
                    f"<td rowspan='2'>6.7{l}</td><td class=''></td><td class=''></td><td class=''></td></tr>"
                    "<tr><td class='small'>&nbsp;</td></tr></tbody>" for l in range(1, 7))   # 展示直後（まだ空欄）
    assert oe.parse(head + empty + "</table>", plain_lane=True)[0] == "unpublished"


def _tsu_rows(msg_lane=None, span=2):
    out = ""
    for l in range(1, 7):
        cells = (f"<td class='col6' colspan='3' rowspan='{span}'>表示するデータがあり\n ません</td>" if l == msg_lane
                 else f"<td>37.{l}0</td><td>4.{l}0</td><td>8.{l}0</td>")
        out += (f"<tr><td rowspan='2' class='col1 waku tei_color{l} '>{l}</td><td>選手</td><td>52.0</td>"
                f"<td>0.0</td><td>6.80</td>{cells}</tr><tr><td class='col3'></td></tr>")
    return "<table><tr><th>枠</th><th>一周</th><th>まわり足</th><th>直線</th></tr>" + out + "</table>"


def test_parse_tsu_no_data_cell():
    st, out, _ = oe.parse(_tsu_rows(msg_lane=3))                      # 1艇だけ「表示するデータがありません」
    assert st == "ok" and out[3] == {"original_lap": None, "original_turn": None, "original_straight": None}
    assert out[1] == {"original_lap": 37.1, "original_turn": 4.1, "original_straight": 8.1}
    # 1つの欄が全艇にまたがる（若松の未公開の形）→ 未公開
    page = _tsu_rows(msg_lane=1, span=12)
    page = re.sub(r"<td>37\.[2-6]0</td><td>4\.[2-6]0</td><td>8\.[2-6]0</td>", "", page)
    st, _, why = oe.parse(page)
    assert st == "unpublished" and "表示するデータ" in why


def test_needs_fetch_retries_old_unsupported_rows():
    old = {"status": "unsupported", "attempts": 0, "reason": "対象外（未確認の場。次の開催時に確認して追加する）"}
    assert oe.needs_fetch(_t(jcd="13"), old, "done", _at("18:00"))           # 対応した場は、締切後でも取る
    assert oe.needs_fetch(_t(jcd="06"), old, "done", _at("15:00"))           # 未確認の場も試す
    assert not oe.needs_fetch(_t(jcd="03"), old, "done", _at("15:00"))       # 確かめて対象外の場は通信しない
    assert not oe.needs_fetch(_t(jcd="24"), old, "done", _at("15:00"))


def test_record_unconfirmed_venue_tries_and_records_reason(monkeypatch):
    calls = []

    def fake_probe(jcd, rk):
        calls.append(jcd)
        return "format", None, "未確認の場で試した: トップページにレース情報の枠が無い", "https://example.invalid/"

    monkeypatch.setattr(oe, "probe", fake_probe)
    sb = FakeSB()
    assert oe.record(sb, _t(jcd="06"), {"status": "unsupported", "attempts": 0}) == "format"
    row = sb.calls[-1][2]["json"]
    assert calls == ["06"] and row["attempts"] == 1 and "未確認の場で試した" in row["reason"]
    assert row["source_url"] == "https://example.invalid/" and row["last_tried_at"]


def test_probe_without_site_does_not_fetch(monkeypatch):
    monkeypatch.setattr(oe, "_get_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("通信しない")))
    st, rows, why, url = oe.probe("99", "20261116_99_1")
    assert st == "format" and rows is None and url is None and "未登録" in why


def test_probe_follows_tabs_and_subtab(monkeypatch):
    top = "<li data-tab=\"group-syussou\"></li><li data-tab=\"group-cyokuzen\"></li><script>typelink = '/modules/yosou/' + t</script>"
    sub = ('<li class="link" data-kind="0"><a href="#">直前情報</a></li>'
           '<li class="link" data-kind="2"><a href="#">オリジナル展示データ</a></li>')
    pages = {"https://www.boatrace-hamanako.jp/": top,
             "https://www.boatrace-hamanako.jp/modules/yosou/group-cyokuzen.php?day=20261116&race=4&if=1": sub,
             "https://www.boatrace-hamanako.jp/modules/yosou/group-cyokuzen.php?day=20261116&race=4&kind=2&if=1":
                 _page(ROWS)}
    seen = []
    monkeypatch.setattr(oe, "_get_text", lambda url, timeout: seen.append(url) or (200, pages[url]))
    st, rows, why, url = oe.probe("06", "20261116_06_4")
    assert st == "ok" and rows[1]["original_lap"] == 36.63 and url.endswith("kind=2&if=1")
    assert seen == list(pages)


def test_run_for_counts_unconfirmed_as_online(monkeypatch):
    done = []
    monkeypatch.setattr(oe, "record", lambda sb, t, p, now=None: done.append(t["jcd"]) or "ok")
    ts = [{**_t(jcd="06", deadline="16:00"), "race_key": f"20261110_06_{i}"} for i in range(1, 9)]
    oe.run_for(FakeSB(), ts, {t["race_key"]: {"status": "done"} for t in ts}, _at("15:00"))
    assert len(done) == oe.MAX_PER_RUN                                        # 通信するので上限に数える
