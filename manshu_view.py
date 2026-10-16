"""
「💥 万舟チャンス（荒れやすいレース）」の表示（manshu.py・pre_picks.py が毎朝選ぶ10レース）。

  - 会場選択: 印のレースがある会場のボタンに 💥と数
  - レース一覧: 印のレースのRカードに 💥
  - 予想タブの日付の下: その日の印のレースの一覧（このレースを開く）。管理者には結果の成績も出す
  - 展示前厳選10: 万舟チャンスにも入っているレースに印
  - 予想画面: 印のレースなら注意書き（本命が来にくい傾向）。管理者には参考として穴寄りの買い目
    （1号艇を1着から外した上位10点・資金配分しない）

表示は「荒れやすいレース」。「儲かる」とは書かない（検証で回収率が100%を超える根拠は無い）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import manshu
from pre_picks_view import _deadline_passed, _get, _open_race, _pct, _yen

JST = ZoneInfo("Asia/Tokyo")
TITLE = f"{manshu.ICON} {manshu.LABEL}"


@st.cache_data(ttl=60, show_spinner=False)
def load_marks(date_iso):
    return _get(f"manshu_chance_picks?race_date=eq.{date_iso}&select=*&order=pick_rank.asc")


@st.cache_data(ttl=600, show_spinner=False)
def load_results():
    return _get("manshu_chance_picks?result_status=eq.settled&select=race_date,trifecta_payout,is_manshu,"
                "main_hit,main_stake,main_return,longshot_hit,longshot_stake,longshot_return"
                "&order=race_date.asc&limit=10000")


def marks_by_key(d):
    """その日の印のレース {race_key: 行}。読めなければ空（画面は印なしで動く）。"""
    try:
        return {r["race_key"]: r for r in load_marks(d.isoformat())}
    except Exception:  # noqa: BLE001
        return {}


def venue_counts(marks):
    """会場ごとの印のレースの数 {jcd: 数}。"""
    out = {}
    for r in marks.values():
        j = str(r["jcd"]).zfill(2)
        out[j] = out.get(j, 0) + 1
    return out


def race_nos(marks, jcd):
    return {int(r["race_no"]) for r in marks.values() if str(r["jcd"]).zfill(2) == str(jcd).zfill(2)}


def longshot_from_final(final, n=manshu.LONGSHOT_N):
    """表示している予想の3連単確率から、1号艇を1着から外した上位 n 点 [{combo, prob}]。"""
    from prediction import trifecta

    tri = trifecta(final)[["combo", "prob"]].assign(race_key="r")
    return manshu.longshot_tickets(tri, n).get("r", [])


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def render_race_note(mark, final=None, model_info=None, is_admin=False):
    """予想画面: 印のレースの注意書き。管理者には参考の穴寄りの買い目。"""
    if not mark:
        return
    pre = float(mark["manshu_prob"])
    post = (model_info or {}).get("manshu_post") or {}
    text = f"{TITLE}　万舟になる確率の目安: 展示前 {pre:.0%}"
    if post.get("prob") is not None:
        text += f" → 展示後 {float(post['prob']):.0%}"
    text += f"（全レースの平均 約{manshu.BASE_RATE:.0%}）"
    st.markdown(
        f'<div style="border:2px solid #ea580c;border-radius:10px;padding:8px 12px;margin:4px 0 6px 0;'
        f'background:rgba(234,88,12,0.08);font-weight:700;">{text}</div>',
        unsafe_allow_html=True,
    )
    st.caption(manshu.NOTE)
    if not is_admin:
        return
    longs = post.get("longshot") or []
    if not longs and final is not None:
        try:
            longs = longshot_from_final(final)
        except Exception:  # noqa: BLE001
            longs = []
    if longs:
        with st.expander("🎲 参考: 穴寄りの買い目（1号艇を1着から外した上位10点・管理者だけ）", expanded=False):
            st.markdown("　".join(f"{t['combo']}（{float(t['prob']):.1%}）" for t in longs))
            st.caption("資金配分はしていません（本線の買い目・投資額には入りません）。各買い目の右は3連単の確率です。"
                       "検証でも回収率が100%を超える根拠はありません。")


def _render_mark(d, r):
    passed = _deadline_passed(d, r.get("deadline"))
    with st.container(border=True):
        head = f"**{r['pick_rank']}. {r['venue']} {r['race_no']}R**　締切 {r.get('deadline') or '--:--'}"
        if passed:
            head += "（締切済み）"
        st.markdown(head)
        line = f"万舟になる確率の目安: 展示前 **{float(r['manshu_prob']):.0%}**"
        if r.get("post_manshu_prob") is not None:
            line += f" → 展示後 **{float(r['post_manshu_prob']):.0%}**"
        st.markdown(line)
        status = r.get("result_status")
        if status == "void":
            st.caption("結果: 中止・不成立")
        elif status == "settled":
            mark = "💥 万舟" if r.get("is_manshu") else "万舟ではない"
            st.caption(f"結果: {mark}　3連単 {r.get('trifecta_actual')}（{_yen(r.get('trifecta_payout'))}）")
        st.button("🎯 このレースを開く", key=f"manshu_open_{r['race_key']}", on_click=_open_race,
                  args=(r["jcd"], r["race_no"]), use_container_width=True)


def _render_summary():
    try:
        rows = load_results()
    except Exception as e:  # noqa: BLE001
        st.caption(f"成績を読めませんでした: {type(e).__name__}")
        return
    if not rows:
        st.caption("結果の出たレースはまだありません（翌朝に記録します）。")
        return
    today = datetime.now(JST).date()
    out = []
    for label, days in (("直近7日", 7), ("直近30日", 30), ("全期間", None)):
        sub = [r for r in rows if days is None or r["race_date"] >= (today - timedelta(days=days)).isoformat()]
        s = manshu.summarize(sub)
        if s:
            out.append({"期間": label, **s})
    t = pd.DataFrame(out)
    for c in t.columns:
        if c.startswith(("万舟率",)) or c.endswith(("的中率", "回収率")):
            t[c] = t[c].map(_pct)
    for c in ("3連単の平均配当", "3連単の配当の中央値"):
        t[c] = t[c].map(_yen)
    st.dataframe(t, hide_index=True, use_container_width=True)
    st.caption(f"全レースの万舟率は約{manshu.BASE_RATE:.0%}（過去2年）。本線 = 展示前の本線の買い目（2,000円）、"
               "穴寄り = 1号艇を1着から外した上位10点（各100円として記録）。中止・不成立のレースは除いています。")


def render_list(d, is_admin=False):
    """予想タブの日付 d の万舟チャンスのレース。"""
    try:
        marks = load_marks(d.isoformat())
    except Exception as e:  # noqa: BLE001
        if is_admin:
            st.caption(f"{TITLE}: 読めませんでした（{type(e).__name__}）")
        return
    if not marks and not is_admin:
        return
    label = TITLE + (f"　{len(marks)}レース" if marks else "")
    with st.expander(label, expanded=False):
        st.caption(
            "展示・直前情報を使わずに毎朝その日の全レースを予想し、3連単が万舟（100円あたり1万円以上）に"
            "なりやすい順に選んだレースです（毎朝8時過ぎに更新）。" + manshu.NOTE
        )
        if not marks:
            st.info("この日の万舟チャンスのレースはありません（毎朝8時過ぎに選びます）。")
        for r in marks:
            _render_mark(d, r)
        if is_admin:
            st.markdown("##### 📈 これまでの成績（管理者だけ）")
            _render_summary()
