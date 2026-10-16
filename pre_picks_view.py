"""
「🌅 展示前厳選10」の表示（管理者だけ。予想タブの日付の下）。

毎朝 pre_picks.py が展示前モデルで選んだ10レース（Supabase の pre_exhibition_picks）を、
会場・レース番号・締切・的中確率・本命・買い目で並べる。「このレースを開く」で予想タブの
下の欄にそのレースを読み込む。展示後にそのレースを予想すると（アプリか自動固定）、買い目は
今の新モデル（展示後）の予想に差し替わり、本命が変わった・的中確率が10ポイント以上下がった
レースには印が付く（外さない）。結果（的中・払戻）は翌朝に記録され、下の成績にまとまる。

表示は「当たりやすいレース」。回収率が上がることを示すものではないので、そうは書かない。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from result_tracker import _supabase_config

JST = ZoneInfo("Asia/Tokyo")
TITLE = "🌅 展示前厳選10"


def _get(path):
    url, key = _supabase_config()
    if not url or not key:
        raise RuntimeError("Supabase の設定がありません")
    r = requests.get(f"{url}/rest/v1/{path}", headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=15)
    r.raise_for_status()
    return r.json() or []


@st.cache_data(ttl=60, show_spinner=False)
def load_picks(date_iso):
    return _get(f"pre_exhibition_picks?race_date=eq.{date_iso}&select=*&order=pick_rank.asc")


@st.cache_data(ttl=600, show_spinner=False)
def load_results():
    return _get("pre_exhibition_picks?result_status=eq.settled&select=race_date,trifecta_payout,"
                "pre_hit,pre_stake,pre_return,post_hit,post_stake,post_return,post_tickets,post_flag"
                "&order=race_date.asc&limit=10000")


# ---------------------------------------------------------------
# 集計（表示と切り離して確かめられるように）
# ---------------------------------------------------------------
def used_result(row):
    """実際に使う買い目の結果: 展示後の予想があればその買い目、無ければ展示前の買い目。"""
    if row.get("post_tickets") and row.get("post_stake") is not None:
        return bool(row.get("post_hit")), int(row.get("post_stake") or 0), int(row.get("post_return") or 0)
    return bool(row.get("pre_hit")), int(row.get("pre_stake") or 0), int(row.get("pre_return") or 0)


def summarize(rows):
    """結果の出たレースの成績。rows は pre_exhibition_picks の行（result_status = settled）。"""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    used = pd.DataFrame([used_result(r) for r in rows], columns=["hit", "stake", "ret"])
    pre_stake = pd.to_numeric(df["pre_stake"], errors="coerce").fillna(0).sum()
    return {
        "日数": int(df["race_date"].nunique()),
        "レース数": int(len(df)),
        "的中率（使った買い目）": float(used["hit"].mean()),
        "回収率（使った買い目）": float(used["ret"].sum() / used["stake"].sum()) if used["stake"].sum() else None,
        "的中率（展示前の買い目）": float(df["pre_hit"].fillna(False).astype(bool).mean()),
        "回収率（展示前の買い目）": float(pd.to_numeric(df["pre_return"], errors="coerce").fillna(0).sum() / pre_stake)
        if pre_stake else None,
        "3連単の平均配当": float(pd.to_numeric(df["trifecta_payout"], errors="coerce").mean()),
        "展示後に印が付いた割合": float(df["post_flag"].fillna(False).astype(bool).mean()),
    }


def _pct(v):
    return "—" if v is None or v != v else f"{v:.1%}"


def _yen(v):
    return "—" if v is None or v != v else f"{int(round(v)):,}円"


def ticket_text(tickets):
    """賭け金のある買い目だけ（資金配分で0円になった候補は出さない）。"""
    return "　".join(f"{t['combo']} {int(t.get('stake') or 0):,}円" for t in (tickets or [])
                    if int(t.get("stake") or 0) > 0)


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def _open_race(jcd, rno):
    st.session_state["selected_jcd"] = str(jcd).zfill(2)
    st.session_state["selected_rno"] = int(rno)
    st.session_state["_pending_race_fetch"] = int(rno)


def _deadline_passed(d, hhmm):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=JST) <= datetime.now(JST)
    except Exception:  # noqa: BLE001
        return False


def _render_pick(d, r, manshu_marks=None):
    post = r.get("post_tickets") is not None
    passed = _deadline_passed(d, r.get("deadline"))
    with st.container(border=True):
        head = f"**{r['pick_rank']}. {r['venue']} {r['race_no']}R**　締切 {r.get('deadline') or '--:--'}"
        if passed:
            head += "（締切済み）"
        st.markdown(head)
        if r["race_key"] in (manshu_marks or {}):
            st.caption("💥 万舟チャンス（荒れやすいレース）にも入っています。本命が来にくい傾向があります。")
        hp = float(r["hit_probability"])
        if post and r.get("post_hit_probability") is not None:
            st.markdown(f"当たりやすさ（この買い目の的中確率）: 展示前 **{hp:.0%}** → 展示後 **{float(r['post_hit_probability']):.0%}**")
        else:
            st.markdown(f"当たりやすさ（この買い目の的中確率）: **{hp:.0%}**")
        fav = f"本命: {r['favorite_lane']}号艇 {r.get('favorite_name') or ''}（1着確率 {float(r.get('favorite_prob') or 0):.0%}）"
        if post and r.get("post_favorite_lane") and int(r["post_favorite_lane"]) != int(r["favorite_lane"]):
            fav += f" → 展示後 {int(r['post_favorite_lane'])}号艇"
        st.markdown(fav)
        if r.get("post_flag"):
            st.warning("⚠️ 展示後に評価が変わりました: " + " / ".join(r.get("post_flag_reasons") or [])
                       + "（厳選からは外していません）")
        if post:
            st.caption("買い目（展示後の予想に差し替え済み）")
            st.markdown(ticket_text(r["post_tickets"]))
        else:
            st.caption("買い目（展示前の予想。展示後にこのレースを予想すると差し替わります）")
            st.markdown(ticket_text(r.get("tickets")))
        status = r.get("result_status")
        if status == "void":
            st.caption("結果: 中止・不成立")
        elif status == "settled":
            hit, stake, ret = used_result(r)
            mark = "✅ 的中" if hit else "❌ 不的中"
            st.caption(f"結果: {mark}　3連単 {r.get('trifecta_actual')}（{_yen(r.get('trifecta_payout'))}）"
                       f"　払戻 {_yen(ret)} / 賭け金 {_yen(stake)}")
        st.button("🎯 このレースを開く", key=f"pre_pick_open_{r['race_key']}", on_click=_open_race,
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
        s = summarize(sub)
        if s:
            out.append({"期間": label, **s})
    t = pd.DataFrame(out)
    for c in t.columns:
        if c.startswith(("的中率", "回収率", "展示後")):
            t[c] = t[c].map(_pct)
    t["3連単の平均配当"] = t["3連単の平均配当"].map(_yen)
    st.dataframe(t, hide_index=True, use_container_width=True)
    st.caption("使った買い目 = 展示後に予想したレースはその買い目、していないレースは展示前の買い目（各2,000円）。"
               "中止・不成立のレースは除いています。")


def render(d, manshu_marks=None):
    """予想タブの日付 d の厳選10レース。manshu_marks: 万舟チャンスのレース {race_key: 行}（印を付ける）"""
    iso = d.isoformat()
    try:
        picks = load_picks(iso)
    except Exception as e:  # noqa: BLE001
        st.caption(f"{TITLE}: 読めませんでした（{type(e).__name__}）")
        return
    label = f"{TITLE}（当たりやすいレース）" + (f"　{len(picks)}レース" if picks else "")
    with st.expander(label, expanded=False):
        st.caption(
            "展示・直前情報を使わずに毎朝その日の全レースを予想し、「この買い目の的中確率」が高い順に選んだ"
            "当たりやすいレースです（毎朝8時過ぎに更新）。払戻や回収率が上がることを示すものではありません。"
        )
        if not picks:
            st.info("この日の厳選レースはありません（毎朝8時過ぎに選びます）。")
        for r in picks:
            _render_pick(d, r, manshu_marks)
        st.markdown("##### 📈 これまでの成績")
        _render_summary()
