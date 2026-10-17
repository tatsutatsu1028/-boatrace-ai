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
from result_tracker import _supabase_config

JST = ZoneInfo("Asia/Tokyo")
TITLE = f"{manshu.ICON} {manshu.LABEL}"


@st.cache_data(ttl=60, show_spinner=False)
def load_marks(date_iso):
    return _get(f"manshu_chance_picks?race_date=eq.{date_iso}&select=*&order=pick_rank.asc")


_RESULT_COLS = ("race_date,trifecta_payout,is_manshu,main_hit,main_stake,main_return,longshot_hit,longshot_stake,"
                "longshot_return")


@st.cache_data(ttl=600, show_spinner=False)
def load_results():
    q = "manshu_chance_picks?result_status=eq.settled&select={}&order=race_date.asc&limit=10000"
    try:
        return _get(q.format(_RESULT_COLS + ",aim_hit,aim_stake,aim_return"))
    except Exception:  # noqa: BLE001  万舟狙いの列がまだ無いデータベース
        return _get(q.format(_RESULT_COLS))


MIGRATION = "supabase/migrations/20261018000000_create_manshu_chance_picks.sql"


def load_error_text(e):
    """読めなかったときの管理者向けの文。保存先の表がまだ無い（マイグレーション前）ときはやることを書く。"""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status == 404:
        return (f"保存先の表がまだありません。Supabase の SQL Editor で `{MIGRATION}` を実行してください"
                "（実行後は翌朝から印が付きます。当日分は Pre-exhibition Picks を task=manshu で動かすと選び直せます）。")
    return f"読めませんでした（{type(e).__name__}{f' {status}' if status else ''}）"


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
# 万舟狙いの買い目（検証中・参考。管理者だけ）
# ---------------------------------------------------------------
REFRESH_SECONDS = 60   # 「オッズを更新」で同じレースを取り直せる間隔（公式サイトへのアクセスを増やさない）


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_odds(date8, jcd, rno, nonce):
    """3連単オッズ（公式サイト）。nonce を変えたときだけ取り直す（1レース1回・更新ボタンのときだけ）。"""
    from official_fetcher import fetch_odds3t

    return manshu.odds_dict(fetch_odds3t(date8, str(jcd).zfill(2), int(rno))), datetime.now(JST).isoformat(
        timespec="seconds")


def _odds_state(race_key):
    return st.session_state.setdefault("manshu_aim_odds", {}).setdefault(race_key, {"nonce": 0, "last": 0.0})


def odds_for(race_key, refresh=False):
    """そのレースのオッズ {組番: オッズ} と取得時刻。refresh で取り直す（REFRESH_SECONDS に1回まで）。"""
    import time

    stt = _odds_state(race_key)
    if refresh and time.time() - stt["last"] >= REFRESH_SECONDS:
        stt["nonce"] += 1
    date8, jcd, rno = race_key.split("_")
    odds, at = _fetch_odds(date8, jcd, int(rno), stt["nonce"])
    if stt["last"] == 0.0 or refresh:
        stt["last"] = time.time()
    return odds, at


def save_aim(race_key, tickets, source, odds_at):
    """表示した万舟狙いの買い目を保存する（締切前・印のレースだけ。同じ内容は保存し直さない）。"""
    import requests

    sig = (race_key, source, tuple((t["combo"], t["odds"]) for t in tickets))
    done = st.session_state.setdefault("manshu_aim_saved", {})
    if done.get(race_key) == sig:
        return None
    url, key = _supabase_config()
    if not url or not key:
        return False
    try:
        r = requests.post(f"{url}/rest/v1/rpc/manshu_save_aim",
                          headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"p_race_key": race_key, "p_tickets": tickets, "p_method": manshu.AIM_METHOD,
                                "p_source": source, "p_odds_at": odds_at}, timeout=15)
        r.raise_for_status()
        ok = bool(r.json())
    except Exception as e:  # noqa: BLE001
        print("[MANSHU] aim save error:", type(e).__name__, flush=True)
        return False
    if ok:
        done[race_key] = sig
        load_marks.clear()
    return ok


def aim_frame(tickets):
    return pd.DataFrame([{"買い目": t["combo"], "確率": f"{float(t['prob']):.1%}", "オッズ": f"{float(t['odds']):.1f}倍",
                          "賭け金": _yen(t["stake"]), "当たった時の払戻": _yen(t["payout"])} for t in tickets])


def normal_frame(tickets, odds):
    """通常の買い目（今のモデル）に、オッズと当たった時の払戻を付ける。"""
    rows = []
    for t in tickets.to_dict("records") if hasattr(tickets, "to_dict") else tickets:
        stake = int(float(t.get("stake") or 0))
        if stake <= 0:
            continue
        o = (odds or {}).get(str(t["combo"]))
        rows.append({"買い目": t["combo"], "確率": f"{float(t.get('prob') or 0):.1%}",
                     "オッズ": f"{o:.1f}倍" if o else "—", "賭け金": _yen(stake),
                     "当たった時の払戻": _yen(stake * o) if o else "—"})
    return pd.DataFrame(rows)


def _aim_caption(odds_at, morning=False):
    st.caption(manshu.AIM_NOTE + (f"（オッズ {odds_at[11:16]} 時点）" if odds_at else ""))
    if morning:
        st.caption(manshu.AIM_MORNING_NOTE)


def _render_aim_in_list(d, r):
    """一覧の1レース: 万舟狙いの買い目（そのレースを開いたときにオッズを1回取る）。"""
    rk = r["race_key"]
    passed = _deadline_passed(d, r.get("deadline"))
    saved = r.get("aim_tickets") or []
    if passed:
        if saved:
            st.caption(f"{manshu.AIM_LABEL}（表示した買い目・{(r.get('aim_odds_at') or '')[11:16]} のオッズ）")
            st.dataframe(aim_frame(saved), hide_index=True, use_container_width=True)
        return
    if not st.toggle(f"💰 {manshu.AIM_LABEL}を見る（オッズを取得）", key=f"manshu_aim_open_{rk}"):
        if saved:
            st.caption(f"保存済みの{manshu.AIM_LABEL}: " + "　".join(t["combo"] for t in saved))
        return
    if not r.get("tri_probs"):
        st.caption("この日は朝の3連単の確率が保存されていないため作れません（次の朝から作れます）。")
        return
    refresh = st.button("🔄 オッズを更新", key=f"manshu_aim_refresh_{rk}")
    try:
        odds, at = odds_for(rk, refresh=refresh)
    except Exception as e:  # noqa: BLE001
        st.caption(f"オッズを取れませんでした（{type(e).__name__}）")
        return
    if not odds:
        st.caption("オッズがまだ出ていません（発売前など）。")
        return
    tickets = manshu.aim_tickets(r["tri_probs"], odds)
    if not tickets:
        st.caption("オッズ100倍以上の組がありません。")
        return
    st.dataframe(aim_frame(tickets), hide_index=True, use_container_width=True)
    _aim_caption(at, morning=True)
    st.caption("確率は朝の展示前の予想。予想タブでこのレースを予想すると、その時点の予想とオッズで作り直します。")
    if save_aim(rk, tickets, "list", at) is False:
        st.caption("（買い目の保存に失敗しました。データベースの準備がまだかもしれません）")


def render_aim_compare(mark, final, tickets, odds_df, is_admin=False):
    """予想タブ: 印のレースで「通常の買い目（今のモデル）」と「万舟狙いの買い目」を並べる（管理者だけ）。"""
    if not mark or not is_admin:
        return
    from prediction import trifecta

    rk = mark["race_key"]
    odds = manshu.odds_dict(odds_df)
    at = None
    fresh = st.session_state.setdefault("manshu_pred_odds", {})
    if st.button("🔄 オッズを更新（万舟狙いの買い目を作り直す）", key=f"manshu_aim_pred_refresh_{rk}"):
        try:
            fresh[rk] = odds_for(rk, refresh=True)
        except Exception as e:  # noqa: BLE001
            st.caption(f"オッズを取れませんでした（{type(e).__name__}）")
    if fresh.get(rk):
        odds, at = fresh[rk]
    if not odds:
        if not st.button("💰 オッズを取得して万舟狙いの買い目を作る", key=f"manshu_aim_pred_{rk}"):
            st.caption(f"{manshu.AIM_LABEL}はオッズが必要です（予想で「3連単オッズも取得」を入れるか、上のボタン）。")
            return
        try:
            odds, at = odds_for(rk)
        except Exception as e:  # noqa: BLE001
            st.caption(f"オッズを取れませんでした（{type(e).__name__}）")
            return
    at = at or datetime.now(JST).isoformat(timespec="seconds")
    aim = manshu.aim_tickets(manshu.tri_dict(trifecta(final)[["combo", "prob"]]), odds)
    st.markdown(f"##### {manshu.ICON} 通常の買い目と{manshu.AIM_LABEL}（管理者だけ）")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**通常の買い目（今のモデル）**")
        nf = normal_frame(tickets, odds)
        if len(nf):
            st.dataframe(nf, hide_index=True, use_container_width=True)
    with c2:
        st.markdown(f"**{manshu.AIM_LABEL}**（各100円・資金配分なし）")
        if aim:
            st.dataframe(aim_frame(aim), hide_index=True, use_container_width=True)
        else:
            st.caption("オッズ100倍以上の組がありません。")
    _aim_caption(at)
    if aim and not _deadline_passed(datetime.fromisoformat(f"{rk[:4]}-{rk[4:6]}-{rk[6:8]}").date(),
                                    mark.get("deadline")):
        if save_aim(rk, aim, "predict", at) is False:
            st.caption("（買い目の保存に失敗しました。データベースの準備がまだかもしれません）")


# ---------------------------------------------------------------
# 結果（その日のうちに書き込まれる。管理者だけ）
# ---------------------------------------------------------------
def cancelled_jcds(d):
    """その日に中止の会場（予想タブが読んだ開催状況。読めていなければ空）。"""
    sched = st.session_state.get(f"daily_schedule_{d.strftime('%Y%m%d')}") or {}
    return {str(k).zfill(2) for k, v in sched.items() if str((v or {}).get("status") or "").strip() == "中止"}


def result_state(d, r, cancelled=()):
    """"settled" / "void"（中止） / "waiting"（結果待ち） / None（締切前）"""
    status = r.get("result_status")
    if status in ("settled", "void"):
        return status
    if str(r.get("jcd")).zfill(2) in set(cancelled):
        return "void"
    if _deadline_passed(d, r.get("deadline")):
        return "waiting"
    return None


def _ox(hit):
    return "○" if hit else "×"


def manshu_result_line(r, state):
    if state == "void":
        return "結果: 中止"
    if state == "waiting":
        return "結果待ち"
    if state != "settled":
        return None
    mark = "💥 万舟" if r.get("is_manshu") else "万舟ではない"
    line = f"結果: 3連単 {r.get('trifecta_actual')}（{_yen(r.get('trifecta_payout'))}）　{mark}"
    if r.get("aim_tickets"):
        line += (f"　{manshu.AIM_LABEL} {_ox(r.get('aim_hit'))} 払戻 {_yen(r.get('aim_return') or 0)}"
                 f" / {_yen(r.get('aim_stake') or 0)}")
    else:
        line += f"　{manshu.AIM_LABEL}: 表示なし"
    return line


def manshu_day_summary(rows, d, cancelled=()):
    """その日の集計（万舟チャンス）。"""
    states = [result_state(d, r, cancelled) for r in rows]
    done = [r for r, s in zip(rows, states) if s == "settled"]
    aim = [r for r in done if r.get("aim_tickets")]
    return {
        "races": len(rows), "settled": len(done), "void": states.count("void"), "waiting": states.count("waiting"),
        "manshu": sum(1 for r in done if r.get("is_manshu")),
        "aim_races": len(aim), "aim_hits": sum(1 for r in aim if r.get("aim_hit")),
        "aim_stake": sum(int(r.get("aim_stake") or 0) for r in aim),
        "aim_return": sum(int(r.get("aim_return") or 0) for r in aim),
    }


def _render_manshu_day_summary(rows, d, cancelled):
    s = manshu_day_summary(rows, d, cancelled)
    st.markdown("##### 🧾 この日の集計（管理者だけ）")
    text = (f"結果の出たレース {s['settled']} / {s['races']}（結果待ち {s['waiting']}・中止 {s['void']}）　"
            f"実際に万舟だったレース **{s['manshu']}** / {s['settled']}")
    st.markdown(text)
    if s["aim_races"]:
        st.markdown(f"{manshu.AIM_LABEL}: **{s['aim_hits']} / {s['aim_races']}レース的中**　"
                    f"合計の払戻 **{_yen(s['aim_return'])}** / 賭け金 {_yen(s['aim_stake'])}")
    else:
        st.caption(f"{manshu.AIM_LABEL}を表示して結果の出たレースはまだありません。")


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def render_race_note(mark, final=None, model_info=None, is_admin=False):
    """予想画面: 印のレースの注意書き（管理者には render_aim_compare で万舟狙いの買い目を並べる）。"""
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


def _render_mark(d, r, is_admin=False, cancelled=()):
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
        if is_admin:
            line = manshu_result_line(r, result_state(d, r, cancelled))
            if line:
                st.markdown(line)
            _render_aim_in_list(d, r)
        elif status == "void":
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
        st.caption("結果の出たレースはまだありません（レースが終わるとその日のうちに記録します）。")
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
               "穴寄り = 1号艇を1着から外した上位10点（各100円として記録）、"
               f"{manshu.AIM_LABEL} = 画面に表示した買い目（表示したレースだけ・各100円）。中止・不成立のレースは除いています。")


def render_list(d, is_admin=False):
    """予想タブの日付 d の万舟チャンスのレース。"""
    try:
        marks = load_marks(d.isoformat())
    except Exception as e:  # noqa: BLE001
        if is_admin:
            st.caption(f"{TITLE}: {load_error_text(e)}")
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
        cancelled = cancelled_jcds(d)
        for r in marks:
            _render_mark(d, r, is_admin=is_admin, cancelled=cancelled)
        if is_admin and marks:
            _render_manshu_day_summary(marks, d, cancelled)
        if is_admin:
            st.markdown("##### 📈 これまでの成績（管理者だけ）")
            _render_summary()
