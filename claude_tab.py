"""
3つの予想（モデルのみ／Claudeのみ／両方の予想平均）と「＋Claude予想」タブ（管理者だけ）。

app_core.py からは次だけ呼ぶ:
  - run_at_predict(...)   予想ボタンを押したとき（管理者だけ）。call_api=True（Claudeのみ・両方の平均）なら
                          Claude を1回だけ呼び、3つの予想をすべて作って保存用の "claude" を返す。
                          call_api=False（モデルのみ）は呼ばず、前に取った読みがあればそれを使う
  - display_tickets(...)  予想タブに出す買い目（押したボタンの予想）
  - render_inline(...)    予想タブに3つの買い目を小さく並べる
  - article_tickets(...)  note・スレッズに使う買い目
  - render_tab(...)       「＋Claude予想」タブの中身
スタッフの操作では、この中の API 呼び出しは一切動かない（呼ぶ側で IS_ADMIN を確かめる）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import claude_reader as cr

SESSION_KEY = "claude_by_race"
CHOICE_KEY = "claude_article_choice"
MODES = ("model", "claude", "avg")
MODE_LABELS = {"model": "モデルのみ予想", "claude": "Claudeのみ予想", "avg": "両方の予想平均"}


def _store():
    return st.session_state.setdefault(SESSION_KEY, {})


def build_tickets(final, work, odds, params, complete_fn, tri=None):
    """AI予想タブと同じ手順（点数の決め方・保険・補充・資金配分）で買い目を作る。tri を渡せばその確率から選ぶ。"""
    from prediction import adaptive_ticket_plan, assess_favorite_risk, rank_tickets, second_favorite_n_for, trifecta
    from stake_allocator import allocate_stakes_smart

    tri = trifecta(final) if tri is None else tri
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


def claude_only_tickets(reading, odds, params):
    """Claude が選んだ買い目をそのまま使い、資金配分だけアプリの仕組みで行う。"""
    from stake_allocator import allocate_stakes_smart

    t = pd.DataFrame(reading["tickets"])[["combo", "group", "prob", "reason"]]
    if odds is not None and len(odds) and {"combo", "odds"}.issubset(odds.columns):
        o = odds[["combo", "odds"]].drop_duplicates("combo", keep="last")
        t = t.merge(o, on="combo", how="left")
        t["expected_return"] = pd.to_numeric(t["prob"], errors="coerce") * pd.to_numeric(t["odds"], errors="coerce")
    return allocate_stakes_smart(
        t, budget=int(params["total_budget"]), unit=100, min_bet=int(params["min_bet"]),
        max_longshot_share=0.15, max_ticket_share=0.35, value_bias=float(params["value_bias"]),
    )


def run_at_predict(ctx, label, work, final, model_tickets, odds, params, complete_fn, api_key,
                   model=None, saved_section=None, call_api=True):
    """
    3つの予想を作り、保存する予想に入れる "claude" の中身を返す（Claude の読みが無ければ None）。
    call_api=False のときは API を呼ばず、このアプリの中・保存済み予想に同じ入力の読みがあれば使う。
    失敗しても例外は出さない。
    """
    from prediction import trifecta
    from stake_allocator import ticket_hit_probability

    reading = cr.read_race(api_key if call_api else "", ctx, work, final, model=model, race_label=label,
                           reuse=cr.reading_from_section(saved_section))
    # 実際に API を呼んだ回だけ使用額を記録する（キャッシュや保存済みの読みを使った回は記録しない）
    if reading.get("called_api") and not reading.get("usage_logged"):
        import claude_usage

        reading["usage_logged"] = claude_usage.log_call(reading, ctx)
    bundle = {"label": label, "reading": reading, "model_final": final,
              "tickets": {"model": model_tickets}, "hit": {"model": ticket_hit_probability(model_tickets)}}
    if reading.get("status") != "ok":
        if call_api:  # 押したのに取れなかったことを画面に出す
            _store()[ctx] = bundle
        return None
    try:
        bundle["tickets"]["claude"] = claude_only_tickets(reading, odds, params)
        bundle["hit"]["claude"] = ticket_hit_probability(bundle["tickets"]["claude"])
    except Exception as e:  # noqa: BLE001
        bundle["error_claude"] = f"{type(e).__name__}: {e}"
    try:
        avg_tri = cr.average_tri(trifecta(final), reading["tickets"])
        avg_final = cr.final_from_tri(final, avg_tri)
        bundle["avg_final"] = avg_final
        bundle["tickets"]["avg"], _ = build_tickets(avg_final, work, odds, params, complete_fn, tri=avg_tri)
        bundle["hit"]["avg"] = ticket_hit_probability(bundle["tickets"]["avg"])
    except Exception as e:  # noqa: BLE001
        bundle["error_avg"] = f"{type(e).__name__}: {e}"
    preds = {k: (bundle["tickets"][k], bundle["hit"].get(k)) for k in ("claude", "avg") if k in bundle["tickets"]}
    section = cr.snapshot_section(reading, model_final=final, predictions=preds)
    if "avg_final" in bundle:
        section["avg_p_first"] = {str(int(r.lane)): round(float(r.p_first), 6)
                                  for r in bundle["avg_final"].itertuples()}
    bundle["section"] = section
    _store()[ctx] = bundle
    return section


def display_tickets(ctx, mode, model_tickets):
    """予想タブに出す買い目と、実際に出す予想の種類（取れなかったときはモデルに戻す）。"""
    b = _store().get(ctx) or {}
    t = (b.get("tickets") or {}).get(mode)
    if mode == "model" or t is None or not len(t):
        return model_tickets, "model"
    t = t.copy()
    t["stake"] = pd.to_numeric(t["stake"], errors="coerce").fillna(0).astype(int)
    return t, mode


def article_tickets(ctx, saved_tickets):
    """note・スレッズに使う買い目（タブで選んだ予想。選んでいなければ予想タブで押した予想）。"""
    b = _store().get(ctx) or {}
    mode = st.session_state.get(CHOICE_KEY, {}).get(ctx) or b.get("mode") or "model"
    t = (b.get("tickets") or {}).get(mode)
    if mode == "model" or t is None or not len(t):
        return saved_tickets
    return t


def set_mode(ctx, mode):
    b = _store().get(ctx)
    if b is not None:
        b["mode"] = mode


# ---------------------------------------------------------------
# 表示
# ---------------------------------------------------------------
def _pct(v):
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _ticket_table(tickets, model_combos=None, with_reason=False):
    t = tickets.copy()
    if model_combos is not None:
        t.insert(0, "違い", ["★" if str(c) not in model_combos else "" for c in t["combo"]])
    cols = [c for c in ("違い", "combo", "group", "prob", "stake") if c in t.columns]
    if with_reason and "reason" in t.columns:
        cols.append("reason")
    t = t[cols]
    if "prob" in t.columns:
        t["prob"] = pd.to_numeric(t["prob"], errors="coerce").map(_pct)
    if "stake" in t.columns:
        t["stake"] = pd.to_numeric(t["stake"], errors="coerce").fillna(0).astype(int).map(lambda v: f"{v:,}円")
    return t.rename(columns={"combo": "3連単", "group": "区分", "prob": "確率", "stake": "金額", "reason": "理由"})


def _first_probs(b):
    final = b["model_final"]
    lanes = pd.to_numeric(final["lane"], errors="coerce").astype(int).tolist()
    mp = dict(zip(lanes, pd.to_numeric(final["p_first"], errors="coerce")))
    reading = b["reading"]
    cp = reading.get("p_first", {}) if reading.get("status") == "ok" else {}
    af = b.get("avg_final")
    ap = dict(zip(pd.to_numeric(af["lane"]).astype(int), af["p_first"])) if af is not None else {}
    return lanes, mp, cp, ap


def render_inline(ctx, model_tickets):
    """予想タブ: 3つの予想の買い目を小さく並べる（Claude が取れなかったときは何も出さない）。"""
    b = _store().get(ctx)
    if not b or b["reading"].get("status") != "ok":
        if b and b["reading"].get("error"):
            st.caption(f"🧠 Claudeの予想は取得できませんでした（{b['reading']['error']}）。モデルの買い目だけを表示しています。")
        return
    lanes, mp, cp, ap = _first_probs(b)
    st.markdown("#### 3つの予想の買い目")
    prob = pd.DataFrame(
        [[_pct(mp.get(ln)) for ln in lanes], [_pct(cp.get(ln)) for ln in lanes],
         [_pct(ap.get(ln)) for ln in lanes]],
        index=["モデル", "Claude", "平均"], columns=[f"{ln}号艇" for ln in lanes])
    st.caption("1着確率")
    st.dataframe(prob, use_container_width=True)
    if b["reading"].get("summary"):
        st.caption(f"🧠 {b['reading']['summary']}")
    model_combos = set(map(str, model_tickets["combo"]))
    cols = st.columns(3)
    for col, mode in zip(cols, MODES):
        with col:
            st.markdown(f"**{MODE_LABELS[mode]}**")
            t = model_tickets if mode == "model" else (b.get("tickets") or {}).get(mode)
            if t is None:
                st.caption("取得できませんでした")
                continue
            st.dataframe(_ticket_table(t, None if mode == "model" else model_combos),
                         use_container_width=True, hide_index=True)
    st.caption("★ = モデルの買い目に無い買い目。詳しい理由は「🤖＋Claude予想」タブ。")


def render_tab(current_ctx):
    st.subheader("🤖＋🧠 ＋Claude予想")
    store = _store()
    if not store:
        st.info("予想タブで「🧠 Claudeのみ予想」か「⚖️ 両方の予想平均」を押すと、そのレースのClaudeの予想と3つの買い目がここに出ます。")
        return
    keys = list(store.keys())
    default = keys.index(current_ctx) if current_ctx in keys else len(keys) - 1
    ctx = st.selectbox("レース（このセッションで予想したレース）", keys, index=default,
                       format_func=lambda k: store[k].get("label") or k, key="claude_tab_race")
    b = store[ctx]
    reading = b["reading"]
    if reading.get("status") != "ok":
        st.warning("Claudeの予想は取得できませんでした。"
                   + (f"（{reading.get('error')}）" if reading.get("error") else ""))
    lanes, mp, cp, ap = _first_probs(b)
    final = b["model_final"]
    model_reason = dict(zip(lanes, final["reason"])) if "reason" in final.columns else {}
    names = dict(zip(lanes, final["racer_name"])) if "racer_name" in final.columns else {}
    rows = [{
        "艇": ln, "選手": names.get(ln, ""),
        "モデル": _pct(mp.get(ln)),
        "Claude": _pct(cp.get(ln)) if cp else "取得できませんでした",
        "平均": _pct(ap.get(ln)) if ap else "-",
        "モデルの理由": model_reason.get(ln, ""),
        "Claudeの理由": reading.get("reasons", {}).get(ln, "") if cp else "",
    } for ln in lanes]
    st.markdown("**1着確率と理由**（平均は3連単の確率を平均した後の1着の確率）")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if reading.get("status") == "ok" and reading.get("summary"):
        st.caption(f"🧠 展開のまとめ：{reading['summary']}")

    tickets = b.get("tickets") or {}
    model_combos = set(map(str, tickets["model"]["combo"])) if tickets.get("model") is not None else set()
    for mode in MODES:
        st.markdown(f"**{MODE_LABELS[mode]}**")
        t = tickets.get(mode)
        if t is None:
            st.caption("取得できませんでした"
                       + (f"（{b.get('error_' + mode)}）" if b.get("error_" + mode) else ""))
            continue
        st.dataframe(_ticket_table(t, None if mode == "model" else model_combos, with_reason=(mode == "claude")),
                     use_container_width=True, hide_index=True)
        st.caption(f"{len(t)}点 ／ 買い目全体の的中確率（補正前）：{_pct(b.get('hit', {}).get(mode))}")
    if tickets.get("avg") is not None:
        st.caption("両方の予想平均：モデルとClaudeの3連単の確率を組み合わせごとに平均し、モデルと同じ手順で買い目を選び直したもの。"
                   "Claudeが挙げなかった組み合わせには、Claudeの確率の残りをモデルの確率の比率で配っています。")

    available = [m for m in MODES if tickets.get(m) is not None]
    if len(available) > 1:
        choices = st.session_state.setdefault(CHOICE_KEY, {})
        current = choices.get(ctx) or b.get("mode") or "model"
        picked = st.radio("記事（note・スレッズ）に使う買い目", available,
                          index=available.index(current) if current in available else 0,
                          format_func=lambda m: MODE_LABELS[m], horizontal=True, key=f"claude_choice_{ctx}")
        choices[ctx] = picked

    usage, cost = reading.get("usage") or {}, reading.get("cost")
    if usage:
        st.caption(
            f"使ったモデル：{reading.get('served_model') or reading.get('model')} ／ "
            f"入力 {usage.get('input_tokens', 0):,}・出力 {usage.get('output_tokens', 0):,} トークン ／ "
            f"約{reading.get('seconds', 0)}秒"
            + (f" ／ 費用 約{cost['jpy']:.1f}円（${cost['usd']:.4f}）" if cost else "")
            + "（このレースで1回だけ。ほかのボタンを押しても呼び直しません）"
        )
