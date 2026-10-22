"""
「🌅 展示前厳選10」の表示（管理者だけ。予想タブの日付の下）。

毎朝 pre_picks.py が展示前モデルで選んだ10レース（Supabase の pre_exhibition_picks）を、
会場・レース番号・締切・的中確率・本命・買い目で並べる。「このレースを開く」で予想タブの
下の欄にそのレースを読み込む。展示後にそのレースを予想すると（アプリか自動固定）、買い目は
今の新モデル（展示後）の予想に差し替わり、本命が変わった・的中確率が10ポイント以上下がった
レースには印が付く（外さない）。結果（的中・払戻）はレースが終わるとその日のうちに書き込まれ（list_results.py）、
各レースと一覧の最後（その日の集計）に出る。翌朝のまとめての記録は取りこぼしの保険。

表示は「当たりやすいレース」。回収率が上がることを示すものではないので、そうは書かない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
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


# ---------------------------------------------------------------
# 一覧の部品（「🌅 展示前厳選10」と「💥 万舟チャンス」で共通。違うところは ListSpec で渡す）
# ---------------------------------------------------------------
@dataclass
class ListSpec:
    title: str                      # 例 "🌅 展示前厳選10（当たりやすいレース）"
    caption: str                    # 一覧の上の説明
    empty: str                      # その日のレースが無いとき
    key: str                        # ボタンの key の頭
    load: Callable                  # date_iso → 行のリスト
    prob_line: Callable             # 行 → 確率の行（markdown）
    tickets_block: Callable         # (d, 行, passed) → 買い目の欄を描く
    result: Callable                # 行 → (的中, 賭け金, 払戻) / None（買い目を表示していない）
    result_extra: Callable = None   # 行 → 結果の行に足す文（万舟だったか など）
    summary_extra: Callable = None  # 結果の出た行のリスト → その日の集計に足す文
    overall: Callable = None        # これまでの成績を描く
    other_marks_note: str = ""      # もう一方の一覧にも入っているレースに出す文
    flag_note: str = "（厳選からは外していません）"
    favorite_line: Callable = None  # 行 → 本命の行（無ければ favorite_line）


def favorite_line(r):
    """本命（展示前）と、展示後に本命が変わったときの艇番。"""
    if r.get("favorite_lane") is None:
        return None
    name = f" {r['favorite_name']}" if r.get("favorite_name") else ""
    fav = f"本命: {r['favorite_lane']}号艇{name}（1着確率 {float(r.get('favorite_prob') or 0):.0%}）"
    post_fav = r.get("post_favorite_lane")
    if post_fav and int(post_fav) != int(r["favorite_lane"]):
        fav += f" → 展示後 {int(post_fav)}号艇"
    return fav


def render_card(d, r, spec, other_marks=None, cancelled=()):
    passed = _deadline_passed(d, r.get("deadline"))
    with st.container(border=True):
        head = f"**{r['pick_rank']}. {r['venue']} {r['race_no']}R**　締切 {r.get('deadline') or '--:--'}"
        if passed:
            head += "（締切済み）"
        st.markdown(head)
        if spec.other_marks_note and r["race_key"] in (other_marks or {}):
            st.caption(spec.other_marks_note)
        st.markdown(spec.prob_line(r))
        fav = (spec.favorite_line or favorite_line)(r)
        if fav:
            st.markdown(fav)
        if r.get("post_flag"):
            st.warning("⚠️ 展示後に評価が変わりました: " + " / ".join(r.get("post_flag_reasons") or [])
                       + spec.flag_note)
        spec.tickets_block(d, r, passed)
        line = list_result_line(r, result_state(d, r, cancelled), spec)
        if line:
            st.markdown(line)
        st.button("🎯 このレースを開く", key=f"{spec.key}_open_{r['race_key']}", on_click=_open_race,
                  args=(r["jcd"], r["race_no"]), use_container_width=True)


def result_state(d, r, cancelled=()):
    """"settled" / "void"（中止） / "waiting"（結果待ち） / None（締切前）。結果はその日のうちに書き込まれる。"""
    status = r.get("result_status")
    if status in ("settled", "void"):
        return status
    if str(r.get("jcd")).zfill(2) in set(cancelled):
        return "void"
    if _deadline_passed(d, r.get("deadline")):
        return "waiting"
    return None


def list_result_line(r, state, spec):
    if state == "void":
        return "結果: 中止"
    if state == "waiting":
        return "結果待ち"
    if state != "settled":
        return None
    line = f"結果: 3連単 {r.get('trifecta_actual')}（{_yen(r.get('trifecta_payout'))}）"
    if spec.result_extra:
        line += f"　{spec.result_extra(r)}"
    res = spec.result(r)
    if res is None:
        return line + "　表示した買い目: なし"
    hit, stake, ret = res
    return line + f"　表示した買い目 {'○' if hit else '×'}　払戻 {_yen(ret)} / 賭け金 {_yen(stake)}"


def list_day_summary(rows, d, spec, cancelled=()):
    """その日の集計（何レース中何レース的中・合計の払戻）。買い目を表示しなかったレースは的中の母数に入れない。"""
    states = [result_state(d, r, cancelled) for r in rows]
    settled = [r for r, s_ in zip(rows, states) if s_ == "settled"]
    done = [res for res in (spec.result(r) for r in settled) if res is not None]
    return {"races": len(rows), "settled": len(settled), "void": states.count("void"),
            "waiting": states.count("waiting"), "with_tickets": len(done), "hits": sum(1 for h, _, _ in done if h),
            "stake": sum(st_ for _, st_, _ in done), "return": sum(rt for _, _, rt in done),
            "extra": spec.summary_extra(settled) if spec.summary_extra else ""}


def _render_day_summary(rows, d, spec, cancelled):
    s = list_day_summary(rows, d, spec, cancelled)
    st.markdown("##### 🧾 この日の集計")
    text = (f"結果の出たレース {s['settled']} / {s['races']}（結果待ち {s['waiting']}・中止 {s['void']}）　"
            f"**{s['hits']} / {s['with_tickets']}レース的中**　合計の払戻 **{_yen(s['return'])}** / 賭け金 {_yen(s['stake'])}")
    if s["with_tickets"] < s["settled"]:
        text += f"（買い目を表示しなかった {s['settled'] - s['with_tickets']}レースを除く）"
    st.markdown(text)
    if s["extra"]:
        st.markdown(s["extra"])


def render_list(d, spec, other_marks=None):
    """一覧（管理者だけ）。予想タブの日付 d のレースを並べ、最後にその日の集計とこれまでの成績。"""
    try:
        rows = spec.load(d.isoformat())
    except Exception as e:  # noqa: BLE001
        st.caption(f"{spec.title}: 読めませんでした（{type(e).__name__}）")
        return
    label = spec.title + (f"　{len(rows)}レース" if rows else "")
    with st.expander(label, expanded=False):
        st.caption(spec.caption)
        if not rows:
            st.info(spec.empty)
        import manshu_view

        cancelled = manshu_view.cancelled_jcds(d)
        for r in rows:
            render_card(d, r, spec, other_marks, cancelled)
        if rows:
            _render_day_summary(rows, d, spec, cancelled)
        if spec.overall:
            st.markdown("##### 📈 これまでの成績")
            spec.overall()


# ---------------------------------------------------------------
# 「🌅 展示前厳選10」
# ---------------------------------------------------------------
def _pre_prob_line(r):
    hp = float(r["hit_probability"])
    if r.get("post_tickets") is not None and r.get("post_hit_probability") is not None:
        return f"当たりやすさ（この買い目の的中確率）: 展示前 **{hp:.0%}** → 展示後 **{float(r['post_hit_probability']):.0%}**"
    return f"当たりやすさ（この買い目の的中確率）: **{hp:.0%}**"


def _pre_tickets_block(d, r, passed):
    if r.get("post_tickets") is not None:
        st.caption("買い目（展示後の予想に差し替え済み）")
        st.markdown(ticket_text(r["post_tickets"]))
    else:
        st.caption("買い目（展示前の予想。展示後にこのレースを予想すると差し替わります）")
        st.markdown(ticket_text(r.get("tickets")))


def result_line(r, state):
    """展示前厳選10の結果の行（list_result_line を展示前厳選10の設定で呼ぶ）。"""
    return list_result_line(r, state, PRE_SPEC)


def day_summary(rows, d, cancelled=()):
    return list_day_summary(rows, d, PRE_SPEC, cancelled)


def _render_summary():
    try:
        rows = load_results()
    except Exception as e:  # noqa: BLE001
        st.caption(f"成績を読めませんでした: {type(e).__name__}")
        return
    if not rows:
        st.caption("結果の出たレースはまだありません（レースが終わるとその日のうちに記録します）。")
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


PRE_SPEC = ListSpec(
    title=f"{TITLE}（当たりやすいレース）",
    caption=("展示・直前情報を使わずに毎朝その日の全レースを予想し、「この買い目の的中確率」が高い順に選んだ"
             "当たりやすいレースです（毎朝8時過ぎに更新）。払戻や回収率が上がることを示すものではありません。"
             "結果はレースが終わるとその日のうちに出ます。"),
    empty="この日の厳選レースはありません（毎朝8時過ぎに選びます）。",
    key="pre_pick",
    load=lambda iso: load_picks(iso),
    prob_line=_pre_prob_line,
    tickets_block=_pre_tickets_block,
    result=used_result,
    overall=_render_summary,
    other_marks_note="💥 万舟チャンス（荒れやすいレース）にも入っています。本命が来にくい傾向があります。",
)


def render(d, manshu_marks=None):
    """予想タブの日付 d の厳選10レース。manshu_marks: 万舟チャンスのレース {race_key: 行}（印を付ける）"""
    render_list(d, PRE_SPEC, manshu_marks)
