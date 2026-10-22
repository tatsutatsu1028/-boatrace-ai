"""
「💥 万舟チャンス（荒れやすいレース）」の表示（manshu.py・pre_picks.py が毎朝選ぶ10レース）。

  - 会場選択: 印のレースがある会場のボタンに 💥と数
  - レース一覧: 印のレースのRカードに 💥
  - 予想タブの日付の下: その日の印のレースの一覧（このレースを開く）。管理者には結果の成績も出す
  - 展示前厳選10: 万舟チャンスにも入っているレースに印
  - 一覧・予想画面の本命: 「本命 1号艇 43%」。展示後に予想していれば展示後の1着確率、無ければ朝の1着確率
  - 予想画面: 印のレースなら注意書き（本命が来にくい傾向）。管理者には参考として穴寄りの買い目
    （1号艇を1着から外した上位10点・資金配分しない）

  - 中穴〜万舟狙いの買い目（管理者だけ）: 1号艇の1着を外し、3連単オッズ30倍以上から確率順に10点。
    朝の買い目は「仮（朝のオッズ）」。展示後に予想タブで予想すると、その時点のオッズで作り直して保存する

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


@st.cache_data(ttl=600, show_spinner=False)
def load_compare():
    """確定オッズで作った朝版・展示後版の買い目の結果（manshu_compare.py が毎晩書く）。"""
    return _get("manshu_aim_compare?select=race_date,is_manshu,pre_hit,pre_stake,pre_return,post_hit,post_stake,"
                "post_return&order=race_date.asc&limit=10000")


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
                          json={"p_race_key": race_key, "p_tickets": tickets, "p_method": manshu.aim_method_name(),
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


def jst_hhmm(ts):
    """保存された時刻（データベースは世界標準時で返す）を日本時間の HH:MM に。読めなければ空。"""
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=JST)
    return t.astimezone(JST).strftime("%H:%M")


def aim_frame(tickets):
    return pd.DataFrame([{"区分": t.get("tier") or manshu.odds_tier(t.get("odds")) or "—",
                          "買い目": t["combo"], "確率": f"{float(t['prob']):.1%}", "オッズ": f"{float(t['odds']):.1f}倍",
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


def prediction_sig(probs):
    """予想の中身の目印（3連単確率が変われば変わる）。予想し直したかどうかの判定に使う。"""
    import hashlib

    text = ",".join(f"{k}:{v:.6f}" for k, v in sorted((probs or {}).items()))
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _aim_caption(odds_at, morning=False):
    st.caption(manshu.AIM_NOTE + (f"（オッズ {jst_hhmm(odds_at)} 時点）" if odds_at else ""))
    if morning:
        st.caption(manshu.AIM_MORNING_NOTE)


def render_aim_compare(mark, final, tickets, odds_df, is_admin=False, pre_exhibition=False):
    """予想タブ: 印のレースで「通常の買い目（今のモデル）」と「万舟狙いの買い目」を並べる（管理者だけ）。
    展示後の予想（pre_exhibition でない）なら、オッズが無くてもその時点のオッズを取りに行き、買い目を作り直して
    保存する（朝の仮の買い目を置き換える。朝のオッズでは30倍以上でも締切前に30倍を下回る組があるため）。"""
    if not mark or not is_admin:
        return
    from prediction import trifecta

    rk = mark["race_key"]
    probs = manshu.tri_dict(trifecta(final)[["combo", "prob"]])
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
    if not odds and not pre_exhibition:
        # 展示後の予想: 予想し直すたびに1回だけ、その時点のオッズを取る（画面を触るたびには取らない）
        sig = (rk, prediction_sig(probs))
        auto = st.session_state.setdefault("manshu_pred_auto", {})
        if auto.get(rk, (None,))[0] != sig:
            try:
                auto[rk] = (sig, odds_for(rk, refresh=True))
            except Exception as e:  # noqa: BLE001
                st.caption(f"オッズを取れませんでした（{type(e).__name__}）")
                return
        odds, at = auto[rk][1]
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
    aim = manshu.aim_tickets(probs, odds)
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
            st.caption("オッズ30倍以上の組がありません。")
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


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def favorite_text(r):
    """本命と1着確率（例: 本命 1号艇 43%（展示後の予想））。展示後に予想していれば展示後、無ければ朝の確率。"""
    fav = manshu.favorite_now(r or {})
    if not fav:
        return None
    lane, p, which = fav
    text = f"本命 {lane}号艇" + (f" {p:.0%}" if p is not None else "") + f"（{which}の予想）"
    morning = (r or {}).get("favorite_lane")
    if which == "展示後" and morning is not None and int(morning) != lane:
        text += f"　朝は {int(morning)}号艇"
    return text


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
    # 今表示している予想の展示後の3連単確率があれば、それで本命を出す（一覧への反映を待たない）
    fav = favorite_text({**mark, "post_tri_probs": post.get("tri") or mark.get("post_tri_probs")})
    if fav:
        text += f"<br>{fav}"
    st.markdown(
        f'<div style="border:2px solid #ea580c;border-radius:10px;padding:8px 12px;margin:4px 0 6px 0;'
        f'background:rgba(234,88,12,0.08);font-weight:700;">{text}</div>',
        unsafe_allow_html=True,
    )
    st.caption(manshu.NOTE)


def _render_mark(d, r):
    """スタッフの画面（今までどおり）: 確率と結果だけ。"""
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
        fav = favorite_text(r)
        if fav:
            st.markdown(fav)
        status = r.get("result_status")
        if status == "void":
            st.caption("結果: 中止・不成立")
        elif status == "settled":
            mark = "💥 万舟" if r.get("is_manshu") else "万舟ではない"
            st.caption(f"結果: {mark}　3連単 {r.get('trifecta_actual')}（{_yen(r.get('trifecta_payout'))}）")
        st.button("🎯 このレースを開く", key=f"manshu_open_{r['race_key']}", on_click=_open_race,
                  args=(r["jcd"], r["race_no"]), use_container_width=True)


# ---------------------------------------------------------------
# 管理者の一覧（「🌅 展示前厳選10」と同じ部品。違うのは確率の行と買い目の欄だけ）
# ---------------------------------------------------------------
def aim_text(tickets):
    """中穴〜万舟狙いの買い目を1行に（区分の印・組番・オッズだけ）。"""
    return "　".join(f"【{t.get('tier') or manshu.odds_tier(t.get('odds')) or '—'}】{t['combo']} {float(t['odds']):.1f}倍"
                    for t in (tickets or []))


MORNING_TAG = "仮（朝のオッズ）"


def aim_source_text(r):
    """保存された買い目の作り方の説明。朝の買い目は「仮（朝のオッズ）」（展示後に予想すると作り直す）。"""
    at = jst_hhmm(r.get("aim_odds_at"))
    src = r.get("aim_source")
    if src == "morning":
        return (f"【{MORNING_TAG}】朝 {at} のオッズで作った仮の買い目です。展示後に予想タブで予想すると、"
                "その時点のオッズで作り直します（「オッズを更新」でも取り直せます）")
    if src == "predict":
        return f"（予想タブで予想した {at} のオッズで作成・保存済み）"
    return f"（{at} のオッズ・保存済み）"


def aim_error_text(r):
    """朝の選定・取り直しで買い目を作れなかったときの文（例: 買い目を作れませんでした（オッズがまだ出ていません）・09:00）。"""
    at = jst_hhmm(r.get("aim_tried_at"))
    return f"買い目を作れませんでした（{r['aim_error']}）" + (f"・{at} に試した結果" if at else "")


def aim_probs(r):
    """買い目を作る3連単の確率: 展示後に予想していればその確率、無ければ朝（展示前）の確率。"""
    if r.get("post_tri_probs"):
        return r["post_tri_probs"], "展示後"
    return r.get("tri_probs"), "展示前"


def _manshu_prob_line(r):
    line = f"万舟になる確率の目安: 展示前 **{float(r['manshu_prob']):.0%}**"
    if r.get("post_manshu_prob") is not None:
        line += f" → 展示後 **{float(r['post_manshu_prob']):.0%}**"
    return line + f"（全レースの平均 約{manshu.BASE_RATE:.0%}）"


def _manshu_tickets_block(d, r, passed):
    rk = r["race_key"]
    saved = r.get("aim_tickets") or []
    if passed:
        if saved:
            st.caption(f"{manshu.AIM_LABEL}（締切前に最後に保存した買い目）{aim_source_text(r)}")
            st.markdown(aim_text(saved))
        elif r.get("aim_error"):
            st.caption(f"{manshu.AIM_LABEL}: {aim_error_text(r)}")
        else:
            st.caption(f"{manshu.AIM_LABEL}: 締切前に表示した買い目はありません")
        return
    probs, which = aim_probs(r)
    shown = st.session_state.setdefault("manshu_aim_list", {})
    label = "🔄 オッズを更新" if (shown.get(rk) or saved) else "💰 オッズを取得して買い目を作る"
    if probs and st.button(label, key=f"manshu_aim_refresh_{rk}"):
        try:
            odds, at = odds_for(rk, refresh=True)
        except Exception as e:  # noqa: BLE001
            st.caption(f"オッズを取れませんでした（{type(e).__name__}）")
            odds, at = {}, None
        if odds:
            tickets = manshu.aim_tickets(probs, odds)
            shown[rk] = (tickets, at, which)
            if tickets and save_aim(rk, tickets, "list", at) is False:
                st.caption("（買い目の保存に失敗しました）")
        elif at is not None:
            st.caption("オッズがまだ出ていません（発売前など）。")
    if rk in shown:
        tickets, at, which = shown[rk]
        st.caption(f"{manshu.AIM_LABEL}（{jst_hhmm(at)} のオッズ・{which}の確率で作成）")
        st.markdown(aim_text(tickets) if tickets else "オッズ30倍以上の組がありません。")
    elif saved:
        st.caption(f"{manshu.AIM_LABEL}{aim_source_text(r)}")
        st.markdown(aim_text(saved))
    elif r.get("aim_error"):
        st.warning(f"{manshu.AIM_LABEL}: {aim_error_text(r)}　上のボタンでその時点のオッズから作り直せます。")
    elif not probs:
        st.caption(f"{manshu.AIM_LABEL}: 朝の3連単の確率が無いため作れません（次の朝から作れます）。")
    else:
        st.caption(f"{manshu.AIM_LABEL}: まだ作っていません（上のボタンでその時点のオッズから作ります）。")
    st.caption(manshu.AIM_NOTE + " " + manshu.AIM_MORNING_NOTE
               + " 当たり外れは、締切前に最後に表示した買い目で判定します。")


def aim_result(r):
    """表示した中穴〜万舟狙いの買い目の結果（表示していなければ None）。"""
    if not r.get("aim_tickets") or r.get("aim_stake") is None:
        return None
    return bool(r.get("aim_hit")), int(r.get("aim_stake") or 0), int(r.get("aim_return") or 0)


def _manshu_summary_extra(settled):
    n = sum(1 for r in settled if r.get("is_manshu"))
    return f"実際に万舟だったレース **{n} / {len(settled)}**"


def _load_marks_list(iso):
    return load_marks(iso)


def _spec():
    from pre_picks_view import ListSpec

    return ListSpec(
        title=f"{TITLE}",
        caption=("展示・直前情報を使わずに毎朝その日の全レースを予想し、3連単が万舟（100円あたり1万円以上）に"
                 "なりやすい順に選んだレースです（毎朝8時過ぎに更新）。" + manshu.NOTE
                 + "結果はレースが終わるとその日のうちに出ます。"),
        empty="この日の万舟チャンスのレースはありません（毎朝8時過ぎに選びます）。",
        key="manshu_list",
        load=_load_marks_list,
        prob_line=_manshu_prob_line,
        tickets_block=_manshu_tickets_block,
        result=aim_result,
        result_extra=lambda r: "💥 万舟" if r.get("is_manshu") else "万舟ではない",
        summary_extra=_manshu_summary_extra,
        overall=_render_summary,
        other_marks_note="🌅 展示前厳選10（当たりやすいレース）にも入っています。",
        flag_note="（万舟チャンスからは外していません）",
        favorite_line=favorite_text,
    )


def manshu_result_line(r, state):
    from pre_picks_view import list_result_line

    return list_result_line(r, state, _spec())


def manshu_day_summary(rows, d, cancelled=()):
    from pre_picks_view import list_day_summary

    return list_day_summary(rows, d, _spec(), cancelled)


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
    _render_compare(today)


def _render_compare(today):
    """確定オッズで作った朝版と展示後版の中穴〜万舟狙いの買い目を、同じレースで並べる。"""
    st.markdown(f"**{manshu.AIM_LABEL}: 朝版と展示後版（確定オッズ・同じレースで比較）**")
    try:
        rows = load_compare()
    except Exception as e:  # noqa: BLE001  表がまだ無いデータベース
        st.caption(f"比較を読めませんでした（{type(e).__name__}）。"
                   "`supabase/migrations/20261022000000_manshu_aim_compare.sql` の実行が必要かもしれません。")
        return
    out = []
    for label, days in (("直近7日", 7), ("直近30日", 30), ("全期間", None)):
        sub = [r for r in rows if days is None or r["race_date"] >= (today - timedelta(days=days)).isoformat()]
        s = manshu.compare_summary(sub)
        if s:
            out.append({"期間": label, **s})
    if not out:
        st.caption("まだありません（レースの翌日以降、事後予想の後に毎晩作ります）。")
        return
    t = pd.DataFrame(out)
    for c in t.columns:
        if c.endswith(("的中率", "回収率")):
            t[c] = t[c].map(_pct)
    st.dataframe(t, hide_index=True, use_container_width=True)
    st.caption("どちらもレース後の確定3連単オッズで、今と同じ作り方（1号艇の1着を外す・中穴5点＋万舟5点・確率順・各100円）で"
               "作った買い目です。朝版 = 朝の3連単の確率（展示前）、展示後版 = 事後予想の3連単の確率（展示・直前情報も使う）。"
               "両方の買い目があるレースだけを数えています。万舟の的中 = 的中したうち3連単が万舟だったレースの数。"
               "確定オッズは締切前に見られるオッズとは違うため、実際に買えた成績ではありません。")


def render_list(d, is_admin=False):
    """予想タブの日付 d の万舟チャンスのレース。管理者は「🌅 展示前厳選10」と同じ一覧、スタッフは今までどおり。"""
    if is_admin:
        try:
            load_marks(d.isoformat())
        except Exception as e:  # noqa: BLE001
            st.caption(f"{TITLE}: {load_error_text(e)}")
            return
        import pre_picks_view

        try:
            pre = {r["race_key"]: r for r in pre_picks_view.load_picks(d.isoformat())}
        except Exception:  # noqa: BLE001
            pre = {}
        pre_picks_view.render_list(d, _spec(), pre)
        return
    try:
        marks = load_marks(d.isoformat())
    except Exception:  # noqa: BLE001
        return
    if not marks:
        return
    with st.expander(TITLE + f"　{len(marks)}レース", expanded=False):
        st.caption(
            "展示・直前情報を使わずに毎朝その日の全レースを予想し、3連単が万舟（100円あたり1万円以上）に"
            "なりやすい順に選んだレースです（毎朝8時過ぎに更新）。" + manshu.NOTE
        )
        for r in marks:
            _render_mark(d, r)
