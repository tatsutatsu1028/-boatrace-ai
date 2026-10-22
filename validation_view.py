"""
「📊 検証」タブの上半分（スマホで見る前提: 上に要約、詳しい表は折りたたみ）。

  1. 要約: 本番モデル（新しいモデル ml-chain）の行だけで、直近7日・30日・切り替え後通算の
     本命的中率・候補内的中率・回収率・レース数。総合・買い目点数別・Brier は折りたたみ
  2. 事後予想（全レース・hindcast_model_daily）: 新モデルと旧モデルを同じレースで比べる
  3. 🌅 展示前厳選10: 日別の的中数／レース数と期間ごとの的中率（予想タブと同じ集計関数）
  4. 💥 万舟チャンス: 万舟率（全レースと並べて）・中穴〜万舟狙いの朝版／展示後版・作り方A〜D（manshu_view）
  5. 予想ボタン別（管理者だけ）: モデルのみ／Claudeのみ／両方の平均
  6. 2番手候補（N=2）: 2026-10-03 の切り替え前後
  7. 表示した的中確率と実際の的中率（買い目全体・本命の1着）

この下（1号艇の確率帯・モデルの切り替え前後・1レース詳細・保存済みレース・手動保存）は app_core.py。
集計は表示と切り離した関数にして tests/test_validation_view.py で確かめる。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st

import manshu_view
import pre_picks_view
from pre_picks_view import PERIODS, _pct, in_period
from prediction import SECOND_FAVORITE_POLICY_SINCE
from result_tracker import (
    MODEL_PERIOD_CURRENT,
    MODEL_PERIOD_ML,
    calibration_table,
    filter_by_model,
    hit_probability_calibration_table,
    load_claude_results,
    load_hindcast_daily,
    metrics,
)

JST = ZoneInfo("Asia/Tokyo")
PROD_PERIODS = (("直近7日", 7), ("直近30日", 30), ("切り替え後通算", None))
# 2番手候補（N=2）を全レースで確保していた期間の始まり（tools/compare_second_favorite_n.py の比較と同じ）
SECOND_FAVORITE_BEFORE_START = "2026-09-08"
HINDCAST_MIGRATION = "supabase/migrations/20261024000000_validation_tab.sql"
MODEL_LABELS = (("new", "新モデル"), ("old", "旧モデル"))


def _today():
    return datetime.now(JST).date()


def _rate(n, d):
    return n / d if d else None


def _count_rate(n, d):
    """件数が少ないうちは件数も見せる: 「45.5%（5/11）」。"""
    return "—" if not d else f"{n / d:.1%}（{n}/{d}）"


def _bool(series):
    return series.astype(str).str.strip().str.lower().isin(["true", "1"])


# ---------------------------------------------------------------
# 1. 要約（本番モデル）
# ---------------------------------------------------------------
def production_rows(df):
    """本番モデル（新しいモデル ml-chain。固定時に保存した版で判定）の行。"""
    return filter_by_model(df, MODEL_PERIOD_ML)


def df_in_period(df, days, today):
    if df is None or days is None:
        return df
    start = (today - timedelta(days=days)).isoformat()
    return df[df["race_date"].astype(str) >= start]


def summary_rows(df, today):
    """期間ごとの レース数・本命的中率・候補内的中率・回収率（率は0〜1）。"""
    out = []
    for label, days in PROD_PERIODS:
        m = metrics(df_in_period(df, days, today))
        out.append({"期間": label, "レース数": m["races"], "本命的中率": m["first_hit_rate"],
                    "候補内的中率": m["candidate_hit_rate"], "回収率": m["roi"]})
    return out


def version_counts(df):
    """含まれている版ごとのレース数と期間（古い版が上）。"""
    if df is None or not len(df):
        return pd.DataFrame(columns=["版", "レース数", "最初", "最後"])
    g = df.groupby(df["model_version"].astype(str)).agg(
        レース数=("race_key", "size"), 最初=("race_date", "min"), 最後=("race_date", "max"))
    return g.reset_index().rename(columns={"model_version": "版"}).sort_values("最初")


def summary_table(rows):
    """スマホで横にはみ出さないよう、行 = 項目・列 = 期間（直近7日 / 直近30日 / 通算）にした表。"""
    cols = {r["期間"].replace("切り替え後通算", "通算"): r for r in rows}
    out = [{"項目": "レース数", **{k: f"{r['レース数']}R" for k, r in cols.items()}}]
    for item in ("本命的中率", "候補内的中率", "回収率"):
        out.append({"項目": item, **{k: _pct(r[item]) for k, r in cols.items()}})
    return pd.DataFrame(out)


def _fmt_rates(t, cols):
    t = t.copy()
    for c in cols:
        t[c] = t[c].map(_pct)
    return t


def render_summary(results_df, today):
    st.markdown("### 📌 要約（本番モデル）")
    prod = production_rows(results_df)
    if prod is None or not len(prod):
        st.info("本番モデル（新しいモデル）で予想したレースの結果はまだありません。")
        return
    st.dataframe(summary_table(summary_rows(prod, today)), hide_index=True, use_container_width=True)
    st.caption("本番の予想モデル（新しいモデル ml-chain）で固定した予想だけ。通算 = 新しいモデルに切り替えてから"
               "今日まで。候補内的中率 = 固定した買い目候補のどれかが当たった割合（賭け金に関係なく）。")

    m = metrics(prod)
    with st.expander("総合・買い目点数別・Brier（切り替え後通算）", expanded=False):
        st.dataframe(pd.DataFrame([
            {"項目": "投資", "値": f"{m['total_stake']:,}円"},
            {"項目": "払戻", "値": f"{m['total_payout']:,}円"},
            {"項目": "収支", "値": f"{m['profit']:+,}円"},
            {"項目": "回収率", "値": _pct(m["roi"])},
            {"項目": "8点内に的中", "値": _pct(m.get("hit_within_8_rate"))},
            {"項目": "9点内に的中", "値": _pct(m.get("hit_within_9_rate"))},
            {"項目": "10点内に的中", "値": _pct(m.get("hit_within_10_rate"))},
            {"項目": "Brier（本命の1着確率）",
             "値": f"{m['brier_first']:.3f}" if pd.notna(m["brier_first"]) else "—"},
        ]), hide_index=True, use_container_width=True)
        st.caption("8〜10点内 = 固定時の候補の上位に実結果が入っていた割合。"
                   "Brier score は小さいほど、予測確率と実際の結果のズレが小さい指標です。")
        st.markdown("**含まれている版**")
        st.dataframe(version_counts(prod), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------
# 2. 事後予想（全レース）
# ---------------------------------------------------------------
def hindcast_period_rows(rows, today):
    """期間 × モデルの成績。両方のモデルで予想した同じレース（paired_*）だけで比べる。"""
    out = []
    for label, days in PERIODS:
        sub = in_period(rows, days, today)
        for model, name in MODEL_LABELS:
            part = [r for r in sub if r.get("model") == model]
            n = sum(int(r.get("paired_races") or 0) for r in part)
            if not n:
                continue
            lost = sum(int(r.get("paired_lane1_lost") or 0) for r in part)
            out.append({
                "期間": label, "モデル": name, "レース数": n,
                "本命的中率": _rate(sum(int(r.get("paired_favorite_hits") or 0) for r in part), n),
                "候補内的中率": _rate(sum(int(r.get("paired_candidate_hits") or 0) for r in part), n),
                "1号艇が負けたR": lost,
                "1号艇負け 候補内的中率": _rate(
                    sum(int(r.get("paired_lane1_lost_candidate_hits") or 0) for r in part), lost),
            })
    return out


def hindcast_daily_frame(rows):
    """日別の候補内的中率（同じレースだけ）。列 = 新モデル / 旧モデル、行 = 日付（古い順）。"""
    data = {}
    for r in rows:
        n = int(r.get("paired_races") or 0)
        if not n:
            continue
        name = dict(MODEL_LABELS).get(r.get("model"))
        if name:
            data.setdefault(str(r["race_date"]), {})[name] = int(r.get("paired_candidate_hits") or 0) / n
    if not data:
        return pd.DataFrame(columns=[n for _, n in MODEL_LABELS])
    return pd.DataFrame.from_dict(data, orient="index").sort_index().reindex(
        columns=[n for _, n in MODEL_LABELS])


def manshu_rate_on(rows, dates):
    """指定した日の全レースの万舟率（その日いちばん多くのレースを予想したモデルの行で数える）。"""
    best = {}
    for r in rows or []:
        d = str(r["race_date"])
        if d in dates and int(r.get("races") or 0) > best.get(d, (0, 0))[0]:
            best[d] = (int(r["races"]), int(r.get("manshu_races") or 0))
    n = sum(v[0] for v in best.values())
    return _rate(sum(v[1] for v in best.values()), n)


def _load_hindcast():
    try:
        return load_hindcast_daily(), None
    except Exception as e:  # noqa: BLE001  ビューがまだ無い（SQL の実行前）など
        return [], e


def render_hindcast(rows, error, today):
    if error is not None:
        with st.expander("🔁 事後予想（全レース）", expanded=False):
            st.caption(f"事後予想の集計を読めませんでした（{type(error).__name__}）。"
                       f"`{HINDCAST_MIGRATION}` の実行が必要かもしれません。")
        return
    table = hindcast_period_rows(rows, today)
    head = ""
    week = {r["モデル"]: r for r in table if r["期間"] == "直近7日"}
    if len(week) == 2:
        head = "　直近7日 候補内 " + " / ".join(f"{n[0]} {_pct(week[n]['候補内的中率'])}"
                                             for n in ("新モデル", "旧モデル"))
    with st.expander("🔁 事後予想（全レース）" + head, expanded=False):
        if not table:
            st.caption("両方のモデルで予想したレースの結果はまだありません。")
            return
        st.dataframe(_fmt_rates(pd.DataFrame(table), ("本命的中率", "候補内的中率", "1号艇負け 候補内的中率")),
                     hide_index=True, use_container_width=True)
        st.caption("レース後に、その時点で分かっていた情報だけで全レースを予想し直した結果（事後予想）。"
                   "新モデル = 新しいモデル（ml-chain・展示後）、旧モデル = 今までのモデル。展示前モデルは含みません。"
                   "両方のモデルで予想した同じレースだけを数えています。1号艇負け = 1着が1号艇以外だったレース。")
        daily = hindcast_daily_frame(rows)
        if len(daily):
            st.markdown("**日別の候補内的中率**")
            st.line_chart(daily * 100)
            with st.expander("日別の表", expanded=False):
                st.dataframe(_fmt_rates(daily.sort_index(ascending=False).rename_axis("日付").reset_index(),
                                        [n for _, n in MODEL_LABELS]),
                             hide_index=True, use_container_width=True)


# ---------------------------------------------------------------
# 3. 展示前厳選10
# ---------------------------------------------------------------
def pre_picks_period_rows(rows, today):
    """期間ごとの 的中数／レース数・的中率・回収率（使った買い目。予想タブの成績と同じ summarize）。"""
    out = []
    for label, days in PERIODS:
        sub = in_period(rows, days, today)
        s = pre_picks_view.summarize(sub)
        if not s:
            continue
        hits = sum(int(pre_picks_view.used_result(r)[0]) for r in sub)
        out.append({"期間": label, "的中": f"{hits}/{s['レース数']}", "的中率": s["的中率（使った買い目）"],
                    "回収率": s["回収率（使った買い目）"]})
    return out


def render_pre_picks(today):
    try:
        rows = pre_picks_view.load_results()
    except Exception as e:  # noqa: BLE001
        with st.expander(pre_picks_view.TITLE, expanded=False):
            st.caption(f"成績を読めませんでした: {type(e).__name__}")
        return
    table = pre_picks_period_rows(rows, today)
    head = next((f"　直近7日 {r['的中']}（{_pct(r['的中率'])}）" for r in table if r["期間"] == "直近7日"), "")
    with st.expander(pre_picks_view.TITLE + head, expanded=False):
        if not table:
            st.caption("結果の出たレースはまだありません。")
            return
        st.dataframe(_fmt_rates(pd.DataFrame(table), ("的中率", "回収率")), hide_index=True, use_container_width=True)
        daily = pd.DataFrame(pre_picks_view.daily_hits(rows))
        daily["的中"] = daily["的中数"].astype(str) + "/" + daily["レース数"].astype(str)
        st.dataframe(_fmt_rates(daily[["日付", "的中", "的中率"]], ("的中率",)), hide_index=True,
                     use_container_width=True)
        st.caption("使った買い目 = 展示後に予想したレースはその買い目、していないレースは展示前の買い目。"
                   "中止・不成立のレースは除いています。予想タブの成績と同じ集計です。")


# ---------------------------------------------------------------
# 4. 万舟チャンス
# ---------------------------------------------------------------
def render_manshu(hindcast_rows, today):
    with st.expander(manshu_view.TITLE, expanded=False):
        manshu_view.render_validation(today, normal_rate=lambda dates: manshu_rate_on(hindcast_rows, dates))


# ---------------------------------------------------------------
# 5. 予想ボタン別（管理者だけ）
# ---------------------------------------------------------------
def _stat(df, first_col, cand_col, stake_col, payout_col):
    def rate(col):
        v = df[col]
        ok = v.notna() & ~v.astype(str).str.strip().str.lower().isin(["", "nan", "none", "null"])
        return _count_rate(int(_bool(v[ok]).sum()), int(ok.sum()))

    stake = pd.to_numeric(df[stake_col], errors="coerce")
    pay = pd.to_numeric(df[payout_col], errors="coerce")
    ok = stake.notna() & (stake > 0)
    return {"本命的中率": rate(first_col), "候補内的中率": rate(cand_col),
            "回収率": _pct(_rate(float(pay[ok].fillna(0).sum()), float(stake[ok].sum()))),
            "投資": int(stake[ok].sum())}


def button_compare_rows(df):
    """Claude の予想があるレースで、予想ボタン別の成績（同じレース）。"""
    if df is None or not len(df):
        return []
    out = []
    for label, cols in (("モデルのみ", ("predicted_first_hit", "candidate_hit", "total_stake", "payout")),
                        ("Claudeのみ", ("claude_first_hit", "claude_candidate_hit", "claude_total_stake",
                                       "claude_payout")),
                        ("両方の平均", ("mix_first_hit", "mix_candidate_hit", "mix_total_stake", "mix_payout"))):
        out.append({"予想": label, "レース数": int(len(df)), **_stat(df, *cols)})
    return out


def render_buttons():
    df = load_claude_results()
    with st.expander(f"🤖 予想ボタン別（管理者だけ）　{len(df)}レース", expanded=False):
        rows = button_compare_rows(df)
        if not rows:
            st.caption("Claude の予想があるレースの結果はまだありません。")
            return
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("Claude の予想を出したレースだけで、同じレースを3通りの予想で比べています（ ）内は 的中数/件数。"
                   "件数が少ないうちは差が偶然でも大きく出ます。")


# ---------------------------------------------------------------
# 6. 2番手候補（N=2）
# ---------------------------------------------------------------
def second_favorite_rows(df):
    """2026-10-03 の切り替え前（全レースN=2）と後（2番手の1着確率0.25以上だけN=2）。
    後は予想モデルでも分ける（10/15 に新しいモデルへ切り替えたため）。率は0〜1。"""
    if df is None or not len(df):
        return []
    d = df["race_date"].astype(str)
    before = df[(d >= SECOND_FAVORITE_BEFORE_START) & (d < SECOND_FAVORITE_POLICY_SINCE)]
    after = df[d >= SECOND_FAVORITE_POLICY_SINCE]
    parts = [
        (f"切り替え前（{SECOND_FAVORITE_BEFORE_START[5:]}〜・全レースN=2）", before),
        ("切り替え後・合計", after),
        ("切り替え後・今までのモデル", filter_by_model(after, MODEL_PERIOD_CURRENT)),
        ("切り替え後・新しいモデル", filter_by_model(after, MODEL_PERIOD_ML)),
    ]
    out = []
    for label, part in parts:
        m = metrics(part)
        cnt = pd.to_numeric(part["candidate_count"], errors="coerce")
        out.append({"期間": label, "レース数": m["races"], "候補内的中率": m["candidate_hit_rate"],
                    "回収率": m["roi"], "平均候補点数": round(float(cnt.mean()), 1) if cnt.notna().any() else None})
    return out


def render_second_favorite(results_df):
    rows = second_favorite_rows(results_df)
    with st.expander("🥈 2番手候補（N=2）の切り替え前後", expanded=False):
        if not rows:
            st.caption("結果の出たレースはまだありません。")
            return
        st.dataframe(_fmt_rates(pd.DataFrame(rows), ("候補内的中率", "回収率")), hide_index=True,
                     use_container_width=True)
        st.caption(f"{SECOND_FAVORITE_POLICY_SINCE} に、2番手の艇を1着にした買い目を全レースで2点確保する（N=2）のをやめ、"
                   "2番手の1着確率が0.25以上のレースだけにしました。日付で分けています。"
                   "切り替え後の途中で予想モデルも変わったため、後はモデルでも分けています。")


# ---------------------------------------------------------------
# 7. 表示した的中確率と実際の的中率
# ---------------------------------------------------------------
def with_shown_probability(df):
    """画面に表示した的中確率（補正後があれば補正後、無ければ補正前）の列 hit_probability_shown を足す。"""
    x = df.copy()
    raw = pd.to_numeric(x.get("hit_probability", pd.Series(np.nan, index=x.index)), errors="coerce")
    cal = pd.to_numeric(x.get("hit_probability_calibrated", pd.Series(np.nan, index=x.index)), errors="coerce")
    x["hit_probability_shown"] = cal.fillna(raw)
    return x


def render_calibration(results_df):
    prod = production_rows(results_df)
    if prod is None:
        prod = pd.DataFrame()
    hit_cal = hit_probability_calibration_table(with_shown_probability(prod), column="hit_probability_shown") \
        if len(prod) else pd.DataFrame()
    head = ""
    if len(hit_cal):
        total = hit_cal[hit_cal["確率帯"] == "全体"].iloc[0]
        head = f"　表示 {total['平均確率']:.1f}% → 実際 {total['実際の的中率']:.1f}%"
    with st.expander("🎯 表示した的中確率と実際の的中率" + head, expanded=False):
        st.markdown("**買い目全体の的中確率**")
        if hit_cal.empty:
            st.caption("的中確率を保存したレースの結果はまだありません。")
        else:
            st.dataframe(hit_cal, hide_index=True, use_container_width=True, column_config={
                "平均確率": st.column_config.NumberColumn(format="%.1f%%"),
                "実際の的中率": st.column_config.NumberColumn(format="%.1f%%"),
                "差": st.column_config.NumberColumn(format="%+.1fpt"),
            })
        st.markdown("**本命の1着確率**")
        cal = calibration_table(prod) if len(prod) else pd.DataFrame()
        if cal.empty:
            st.caption("まだありません。")
        else:
            st.dataframe(cal, hide_index=True, use_container_width=True)
        st.caption("本番モデルで固定した予想だけ。買い目全体 = 画面に表示した「この買い目の的中確率」と、"
                   "買い目のどれかが実際に当たった割合。本命 = 本命の1着確率と、本命が実際に1着だった割合。"
                   "差がプラスなら表示より当たっている、マイナスなら表示ほど当たっていません。")


# ---------------------------------------------------------------
# まとめて描く
# ---------------------------------------------------------------
def render(results_df, is_admin=False):
    """検証タブの 1〜7。results_df は prediction_results の一覧（読めなかったときは None）。"""
    today = _today()
    if results_df is not None:
        render_summary(results_df, today)
    hindcast_rows, hindcast_error = _load_hindcast()
    st.markdown("### 🔎 詳しく見る")
    render_hindcast(hindcast_rows, hindcast_error, today)
    render_pre_picks(today)
    render_manshu(hindcast_rows, today)
    if is_admin:
        render_buttons()
    if results_df is not None:
        render_second_favorite(results_df)
        render_calibration(results_df)
