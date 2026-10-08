"""
「＋Claude予想」タブ（管理者だけ）と、「AI最終予想」を押したときの Claude の処理。

app_core.py からは次の3か所だけ呼ぶ:
  - run_at_predict(...)   「AI最終予想」を押したとき（管理者だけ）。Claude の読みと
                          モデル＋Claude の買い目を作り、保存する予想に入れる "claude" を返す
  - render_tab(...)       「＋Claude予想」タブの中身（管理者だけタブを作る）
  - article_tickets(...)  note・スレッズに使う買い目（タブで「モデル＋Claude」を選んだレースだけ差し替え）
スタッフの操作では、この中の API 呼び出しは一切動かない（呼ぶ側で IS_ADMIN を確かめる）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import claude_reader as cr

SESSION_KEY = "claude_by_race"
CHOICE_KEY = "claude_article_choice"
CHOICE_MODEL = "モデルの買い目"
CHOICE_MIX = "モデル＋Claudeの買い目"


def _store():
    return st.session_state.setdefault(SESSION_KEY, {})


def build_tickets(final, work, odds, params, complete_fn):
    """AI予想タブと同じ手順（点数の決め方・保険・補充・資金配分）で買い目を作る。"""
    from prediction import adaptive_ticket_plan, assess_favorite_risk, rank_tickets, second_favorite_n_for, trifecta
    from stake_allocator import allocate_stakes_smart

    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main = min(int(plan["main_n"]), target)
    cover = target - main
    favorite_lane, risk_score, _ = assess_favorite_risk(work, final)
    hedge = favorite_lane if (params["hedge_enabled"] and risk_score >= 2) else None
    tickets = rank_tickets(
        tri, odds, main_n=main, cover_n=cover, longshot_n=0,
        longshot_min_prob=float(params["longshot_min_prob_pct"]) / 100.0, hedge_lane=hedge,
        first=final, min_second_coverage=plan["min_second_coverage"], close_third_gap=None,
        close_third_coverage=4, second_favorite_n=second_favorite_n_for(final),
    )
    tickets = complete_fn(tickets, tri, odds, main_points=main, cover_points=cover)
    tickets = allocate_stakes_smart(
        tickets, budget=int(params["total_budget"]), unit=100, min_bet=int(params["min_bet"]),
        max_longshot_share=0.15, max_ticket_share=0.35, value_bias=float(params["value_bias"]),
        guarantee_col="second_favorite",
    )
    return tickets, plan


def run_at_predict(ctx, label, work, final, odds, params, complete_fn, api_key, model=None, saved_section=None):
    """
    Claude の読み → モデル＋Claude の買い目。結果はセッションに残し（タブで表示）、
    保存する予想に入れる "claude" の中身を返す。失敗しても例外は出さない。
    """
    reading = cr.read_race(api_key, ctx, work, final, model=model, race_label=label,
                           reuse=cr.reading_from_section(saved_section))
    bundle = {"label": label, "reading": reading, "model_final": final}
    mix_tickets, hit_prob = None, None
    if reading.get("status") == "ok":
        try:
            from stake_allocator import ticket_hit_probability

            mix = cr.mix_final(final, reading["p_first"])
            mix_tickets, plan = build_tickets(mix, work, odds, params, complete_fn)
            hit_prob = ticket_hit_probability(mix_tickets)
            bundle.update({"mix_final": mix, "mix_tickets": mix_tickets, "mix_plan": plan,
                           "mix_hit_probability": hit_prob})
        except Exception as e:  # noqa: BLE001
            bundle["mix_error"] = f"{type(e).__name__}: {e}"
    section = cr.snapshot_section(reading, model_final=final, mix_tickets=mix_tickets,
                                  mix_hit_probability=hit_prob)
    bundle["section"] = section
    _store()[ctx] = bundle
    return section


def attach_model_tickets(ctx, tickets, hit_probability):
    """AI予想タブで作ったモデルの買い目を、タブで並べて表示するために残す。"""
    b = _store().get(ctx)
    if b is not None:
        b["model_tickets"] = tickets
        b["model_hit_probability"] = hit_probability


def article_tickets(ctx, saved_tickets):
    """note・スレッズに使う買い目。タブで「モデル＋Claude」を選んだレースだけ差し替える。"""
    if st.session_state.get(CHOICE_KEY, {}).get(ctx) != CHOICE_MIX:
        return saved_tickets
    b = _store().get(ctx) or {}
    mix = b.get("mix_tickets")
    return mix if mix is not None and len(mix) else saved_tickets


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def _pct(v):
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _ticket_table(tickets):
    t = tickets.copy()
    cols = [c for c in ("combo", "group", "prob", "stake") if c in t.columns]
    t = t[cols]
    if "prob" in t.columns:
        t["prob"] = pd.to_numeric(t["prob"], errors="coerce").map(_pct)
    if "stake" in t.columns:
        t["stake"] = pd.to_numeric(t["stake"], errors="coerce").fillna(0).astype(int).map(lambda v: f"{v:,}円")
    return t.rename(columns={"combo": "3連単", "group": "区分", "prob": "確率", "stake": "金額"})


def render_tab(current_ctx):
    st.subheader("🤖＋🧠 ＋Claude予想")
    store = _store()
    if not store:
        st.info("予想タブで「🤖 AI最終予想」を押すと、そのレースのClaudeの読みとモデル＋Claudeの買い目がここに出ます。")
        return
    keys = list(store.keys())
    default = keys.index(current_ctx) if current_ctx in keys else len(keys) - 1
    ctx = st.selectbox("レース（このセッションで予想したレース）", keys, index=default,
                       format_func=lambda k: store[k].get("label") or k, key="claude_tab_race")
    b = store[ctx]
    reading = b["reading"]
    final = b["model_final"]

    if reading.get("status") != "ok":
        st.warning("Claudeの読みは取得できませんでした。"
                   + (f"（{reading.get('error')}）" if reading.get("error") else ""))
    lanes = pd.to_numeric(final["lane"], errors="coerce").astype(int).tolist()
    model_p = dict(zip(lanes, pd.to_numeric(final["p_first"], errors="coerce")))
    model_reason = dict(zip(lanes, final["reason"])) if "reason" in final.columns else {}
    names = dict(zip(lanes, final["racer_name"])) if "racer_name" in final.columns else {}
    rows = []
    for ln in lanes:
        cp = reading.get("p_first", {}).get(ln) if reading.get("status") == "ok" else None
        rows.append({
            "艇": ln,
            "選手": names.get(ln, ""),
            "モデル": _pct(model_p.get(ln)),
            "Claude": _pct(cp) if cp is not None else "取得できませんでした",
            "平均": _pct((model_p.get(ln, 0) + cp) / 2) if cp is not None else "-",
            "モデルの理由": model_reason.get(ln, ""),
            "Claudeの理由": reading.get("reasons", {}).get(ln, "") if cp is not None else "",
        })
    st.markdown("**1着確率と理由**")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if reading.get("status") == "ok" and reading.get("summary"):
        st.caption(f"🧠 Claudeの見立て：{reading['summary']}")

    st.markdown("**買い目**")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{CHOICE_MODEL}**")
        if b.get("model_tickets") is not None:
            st.dataframe(_ticket_table(b["model_tickets"]), use_container_width=True, hide_index=True)
            st.caption(f"買い目全体の的中確率（補正前）：{_pct(b.get('model_hit_probability'))}")
        else:
            st.caption("モデルの買い目はAI予想タブを見てください。")
    with c2:
        st.markdown(f"**{CHOICE_MIX}**")
        if b.get("mix_tickets") is not None:
            st.dataframe(_ticket_table(b["mix_tickets"]), use_container_width=True, hide_index=True)
            st.caption(f"買い目全体の的中確率：{_pct(b.get('mix_hit_probability'))}"
                       "（1着は平均、2着・3着はモデルの条件付き確率）")
        else:
            st.caption("取得できませんでした" + (f"（{b['mix_error']}）" if b.get("mix_error") else ""))

    if b.get("mix_tickets") is not None:
        choices = st.session_state.setdefault(CHOICE_KEY, {})
        picked = st.radio("記事（note・スレッズ）に使う買い目", [CHOICE_MODEL, CHOICE_MIX],
                          index=1 if choices.get(ctx) == CHOICE_MIX else 0, horizontal=True,
                          key=f"claude_choice_{ctx}")
        choices[ctx] = picked

    usage, cost = reading.get("usage") or {}, reading.get("cost")
    if usage:
        st.caption(
            f"使ったモデル：{reading.get('served_model') or reading.get('model')} ／ "
            f"入力 {usage.get('input_tokens', 0):,}・出力 {usage.get('output_tokens', 0):,} トークン ／ "
            f"約{reading.get('seconds', 0)}秒"
            + (f" ／ 費用の目安 約{cost['jpy']:.1f}円（${cost['usd']:.4f}）" if cost else "")
            + "（同じレース・同じ入力なら再実行しても呼び直しません）"
        )


# ---------------------------------------------------------------
# 予想タブに小さく添える比較（管理者だけ。Claude が取れたときだけ）
# ---------------------------------------------------------------
def _side_table(tickets, other_combos):
    t = tickets.copy()
    t["違い"] = ["★" if str(c) not in other_combos else "" for c in t["combo"]]
    t = t[["違い", "combo", "group", "stake"]] if "stake" in t.columns else t[["違い", "combo", "group"]]
    if "stake" in t.columns:
        t["stake"] = pd.to_numeric(t["stake"], errors="coerce").fillna(0).astype(int).map(
            lambda v: f"{v:,}円" if v else "0円")
    return t.rename(columns={"combo": "3連単", "group": "区分", "stake": "金額"})


def render_inline(ctx, model_tickets):
    """モデルの買い目とモデル＋Claudeの買い目を左右に並べ、片方にしか無い買い目に★を付ける。"""
    b = _store().get(ctx)
    if not b or b["reading"].get("status") != "ok" or b.get("mix_tickets") is None:
        return
    reading, final, mix = b["reading"], b["model_final"], b["mix_tickets"]
    model_combos = set(map(str, model_tickets["combo"]))
    mix_combos = set(map(str, mix["combo"]))
    st.markdown("#### 🤖 モデル と 🤖＋🧠 モデル＋Claude の買い目")
    lanes = pd.to_numeric(final["lane"], errors="coerce").astype(int).tolist()
    mp = dict(zip(lanes, pd.to_numeric(final["p_first"], errors="coerce")))
    cp = reading["p_first"]
    prob = pd.DataFrame(
        [[_pct(mp.get(ln)) for ln in lanes], [_pct(cp.get(ln)) for ln in lanes],
         [_pct((mp.get(ln, 0) + cp.get(ln, 0)) / 2) for ln in lanes]],
        index=["モデル", "Claude", "平均"], columns=[f"{ln}号艇" for ln in lanes])
    st.dataframe(prob, use_container_width=True)
    if reading.get("summary"):
        st.caption(f"🧠 {reading['summary']}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{CHOICE_MODEL}**")
        st.dataframe(_side_table(model_tickets, mix_combos), use_container_width=True, hide_index=True)
    with c2:
        st.markdown(f"**{CHOICE_MIX}**")
        st.dataframe(_side_table(mix, model_combos), use_container_width=True, hide_index=True)
    diff = len(model_combos ^ mix_combos)
    st.caption("★ = もう一方には無い買い目" + (f"（違いは{diff}点）" if diff else "（買い目は同じ）")
               + "。詳しい理由は「🤖＋Claude予想」タブ。")
