"""
予想タブの「🤖 モデルのみ予想」と同じ予想・買い目を出す（Streamlit を使わない部分）。

アプリ（app_core.py）と、展示後の自動予想（auto_post_predict.py）の両方がこれを呼ぶので、
同じレース・同じ設定なら同じ予想と買い目になる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import ml_live
from prediction import (
    adaptive_ticket_plan,
    assess_favorite_risk,
    predict,
    rank_tickets,
    research_prediction_variants,
    second_favorite_n_for,
    trifecta,
)
from stake_allocator import allocate_stakes_smart


def complete_adaptive_tickets(tickets, tri, odds, main_points, cover_points):
    """予定点数に足りない買い目を、未採用の確率上位から補充する。"""
    out = tickets.copy().drop_duplicates("combo", keep="first").reset_index(drop=True)
    pool = tri.copy().drop_duplicates("combo", keep="first")
    pool["prob"] = pd.to_numeric(pool["prob"], errors="coerce").fillna(0.0)

    if odds is not None and len(odds) and {"combo", "odds"}.issubset(odds.columns):
        odds_unique = odds[["combo", "odds"]].drop_duplicates("combo", keep="last")
        pool = pool.merge(odds_unique, on="combo", how="left")
    else:
        pool["odds"] = np.nan
    pool["odds"] = pd.to_numeric(pool["odds"], errors="coerce")
    pool["expected_return"] = pool["prob"] * pool["odds"]

    recommended = True
    if "recommended" in out.columns and len(out):
        recommended = bool(out["recommended"].fillna(True).all())

    for group, target in (("本線", int(main_points)), ("抑え", int(cover_points))):
        missing = target - int(out["group"].eq(group).sum())
        if missing <= 0:
            continue
        picks = (
            pool[~pool["combo"].isin(set(out["combo"]))]
            .sort_values("prob", ascending=False)
            .head(missing)
            .copy()
        )
        picks["group"] = group
        if "recommended" in out.columns:
            picks["recommended"] = recommended
        out = pd.concat([out, picks], ignore_index=True)

    return out


def model_only_prediction(model, work, odds, params, model_setting, date_key, jcd, rno):
    """
    「モデルのみ予想」の予想と買い目。

    model         : prediction.train(history) の今までのモデル（新しいモデルが使えないときの予想・比較用）
    work          : そのレースの1艇1行（出走表・展示・直前情報）
    odds          : 3連単オッズ（combo, odds）。無くてもよい
    params        : display_weight / weather_weight / venue_course_weight / hedge_enabled /
                    longshot_min_prob_pct / total_budget / min_bet / value_bias
    model_setting : app_settings の予想モデルの設定（ml_live.production_final に渡す）

    戻り値 dict: before, before_label, model_info, final, tickets, hedge_lane, risk_reasons,
                 research_variants, ticket_plan
    """
    display_weight = float(params["display_weight"])
    weather_weight = float(params["weather_weight"])
    venue_course_weight = float(params["venue_course_weight"])

    pre = work.copy()
    # 比較用の「展示反映前」は通常展示タイムだけをOFFにする。
    # オリジナル展示（直線・まわり足・1周）は本番予想に使わない。
    # 天候・場特性・今節・コース・級別・決まり手は最終予想と同条件にする。
    pre["exhibition_time"] = np.nan
    pre["original_straight"] = np.nan
    pre["original_turn"] = np.nan
    pre["original_lap"] = np.nan
    pre["exhibition_st"] = np.nan
    before = predict(
        model,
        pre,
        display_weight=0,
        weather_weight=weather_weight,
        venue_course_weight=venue_course_weight,
        original_display_scale=0.0,
    )
    final = predict(
        model,
        work,
        display_weight=display_weight,
        weather_weight=weather_weight,
        venue_course_weight=venue_course_weight,
        original_display_scale=0.0,
    )
    # app_settings.prediction_model が新しいモデルなら、そのモデルの予想を
    # 本番に使う（自動固定と同じ ml_live.production_final）。使えなかった
    # レース（過去のレース・集計表が古い など）は今のモデルの予想のまま。
    current_final = final
    ml_final, model_info = ml_live.production_final(model_setting, work, date_key, jcd, rno)
    before_label = None
    if ml_final is not None:
        final = ml_final
        # 新しいモデルは展示前だけの予想を出せないので、今までのモデルと並べる
        before = current_final
        before_label = "今までのモデル"

    # 研究用の比較は別計算。final（本番予想）は一切変更しない。
    research_variants = research_prediction_variants(
        model,
        work,
        display_weight=display_weight,
        weather_weight=weather_weight,
        venue_course_weight=venue_course_weight,
    )
    if ml_final is not None:
        research_variants = {"今までのモデル": current_final, **(research_variants or {})}
    tri = trifecta(final)
    ticket_plan = adaptive_ticket_plan(final)
    target_points = int(ticket_plan["point_count"])
    main_points = min(int(ticket_plan["main_n"]), target_points)
    cover_points = target_points - main_points

    favorite_lane, risk_score, risk_reasons = assess_favorite_risk(work, final)
    hedge_lane = favorite_lane if (params["hedge_enabled"] and risk_score >= 2) else None

    tickets = rank_tickets(
        tri,
        odds,
        main_n=main_points,
        cover_n=cover_points,
        longshot_n=0,
        longshot_min_prob=float(params["longshot_min_prob_pct"]) / 100.0,
        hedge_lane=hedge_lane,
        first=final,
        min_second_coverage=ticket_plan["min_second_coverage"],
        close_third_gap=None,
        close_third_coverage=4,
        second_favorite_n=second_favorite_n_for(final),
    )
    tickets = complete_adaptive_tickets(
        tickets,
        tri,
        odds,
        main_points=main_points,
        cover_points=cover_points,
    )
    if len(tickets) != target_points:
        raise RuntimeError(
            f"買い目点数の生成不整合: 予定{target_points}点 / 実際{len(tickets)}点"
        )
    tickets = allocate_stakes_smart(
        tickets,
        budget=int(params["total_budget"]),
        unit=100,
        min_bet=int(params["min_bet"]),
        max_longshot_share=0.15,
        max_ticket_share=0.35,
        value_bias=float(params["value_bias"]),
        guarantee_col="second_favorite",
    )
    return {
        "before": before,
        "before_label": before_label,
        "model_info": model_info,
        "final": final,
        "tickets": tickets,
        "hedge_lane": hedge_lane,
        "risk_reasons": risk_reasons,
        "research_variants": research_variants,
        "ticket_plan": ticket_plan,
    }
