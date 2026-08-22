import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from official_fetcher import VENUES, fetch_official_race, fetch_odds3t, fetch_race_result
from today_schedule_fetcher import fetch_today_schedule
from prediction import train, predict, trifecta, rank_tickets, confidence, assess_favorite_risk, research_prediction_variants
from stake_allocator import allocate_stakes_smart
from original_exhibition_ocr import extract_original_exhibition, OCR_AVAILABLE
from result_tracker import (
    load_results,
    save_race_result,
    delete_result,
    result_exists,
    metrics as validation_metrics,
    calibration_table,
    load_settings,
    save_settings,
    odds_tracking_available,
    add_to_odds_watchlist,
    save_odds_snapshot_now,
    load_odds_history,
    snapshot_payout_from_official,
    deactivate_odds_watchlist,
    load_prediction_snapshot,
    save_prediction_snapshot,
)
st.set_page_config(page_title="BOAT AI Mobile", page_icon="🚤", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:.8rem;padding-bottom:5rem}
.stButton>button{width:100%;min-height:3rem;border-radius:14px;font-weight:700}
.ticket{padding:.8rem 1rem;border:1px solid rgba(150,150,150,.28);border-radius:14px;margin:.45rem 0}
.small{opacity:.72;font-size:.86rem}
.money{margin-top:.45rem;font-size:1.02rem;font-weight:700}
/* 会場グリッドはスマホ幅でもStreamlit標準の縦積みにせず、
   横4列のまま表示する（狭い画面でも横スクロールなしで一覧できるように）。 */
div[class*="st-key-venue_grid"] [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:.35rem!important}
div[class*="st-key-venue_grid"] [data-testid="column"],
div[class*="st-key-venue_grid"] [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;width:auto!important;padding:0 .15rem!important}
div[class*="st-key-venue_grid"] .stButton>button{min-height:2.7rem;font-size:.76rem;padding:.3rem .15rem;white-space:normal;line-height:1.15}
div[class*="st-key-venue_grid"] .stCaption{text-align:center;font-size:.68rem}
</style>
""", unsafe_allow_html=True)

st.title("🚤 BOAT AI Mobile")
st.caption("公式情報＋展示データを合わせて、3連単を『本線・抑え・穴』に整理")

@st.cache_data
def load_demo_history():
    return pd.read_csv(Path(__file__).parent / "sample_history.csv")

def safe_name(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s

def ensure_columns(df, defaults):
    out = df.copy()
    for c, default in defaults.items():
        if c not in out.columns:
            out[c] = default
    return out

def race_key(d, jcd, rno):
    return f"{d.strftime('%Y%m%d')}_{jcd}_{int(rno)}"

@st.cache_data(ttl=1800, show_spinner=False)
def cached_fetch_race(date_str, jcd, rno):
    """
    公式データ取得結果をサーバー側（プロセス単位）でキャッシュする。

    スマホのブラウザはバックグラウンド化などでWebSocket接続が切れやすく、
    再接続時にst.session_stateがリセットされることがある。
    session_stateだけに頼ると「せっかく取得したデータが消えた」状態に
    なるため、同じレース（date/jcd/rno）の再取得はここから即座に
    復元できるようにしておく。
    """
    return fetch_official_race(date_str, jcd, rno)

@st.cache_data(ttl=1800, show_spinner=False)
def cached_fetch_odds(date_str, jcd, rno):
    return fetch_odds3t(date_str, jcd, rno)

@st.cache_data(ttl=600, show_spinner=False)
def cached_fetch_schedule(date_str):
    """
    本日開催中の会場一覧（開催日目・発売状況）。
    レース1件分の取得よりは軽いページだが、会場グリッドを
    表示するたびに毎回叩かないよう10分キャッシュする。
    """
    return fetch_today_schedule(date_str)

def missing_summary(df):
    checks = [
        ("racer_win_rate", "全国勝率"),
        ("local_win_rate", "当地勝率"),
        ("motor_2ren", "モーター2連率"),
        ("boat_2ren", "ボート2連率"),
        ("avg_st", "平均ST"),
    ]
    out = []
    for col, label in checks:
        n = pd.to_numeric(df.get(col, pd.Series(dtype=float)), errors="coerce").notna().sum()
        if n < 6:
            out.append(f"{label}({n}/6)")
    return out

st.session_state.setdefault("race", None)
st.session_state.setdefault("odds", None)
st.session_state.setdefault("race_context", None)

tab1, tab2, tab3, tab4 = st.tabs(["🎯 予想", "🧠 学習データ", "⚙️ 設定", "📊 検証"])

with tab2:
    st.subheader("学習データ")
    hist_up = st.file_uploader("過去成績CSV", type="csv", key="hist")
    history = pd.read_csv(hist_up) if hist_up else load_demo_history()
    if hist_up:
        st.success(f"アップロードした学習データ：{len(history):,}行")
    else:
        st.warning(f"動作確認用の合成データ {len(history):,}行を使用中。実運用前に公式過去データへ置き換えてください。")
    with st.expander("学習データ先頭を見る"):
        st.dataframe(history.head(12), use_container_width=True, hide_index=True)

with tab3:
    st.subheader("買い目設定")

    if "app_settings" not in st.session_state:
        st.session_state["app_settings"] = load_settings()
    saved = st.session_state["app_settings"]

    main_n = st.number_input("🔥 本線 点数", 1, 10, int(saved.get("main_n", 3)))
    cover_n = st.number_input("🛟 抑え 点数", 0, 10, int(saved.get("cover_n", 3)))
    hole_n = st.number_input("💎 穴 点数", 0, 10, int(saved.get("hole_n", 2)))

    st.divider()
    total_budget = st.number_input(
        "💴 1レース予算",
        min_value=500,
        max_value=100000,
        value=int(saved.get("total_budget", 2000)),
        step=100,
    )
    min_bet = st.number_input(
        "1点あたり最低購入額",
        min_value=100,
        max_value=1000,
        value=int(saved.get("min_bet", 100)),
        step=100,
    )
    longshot_min_prob_pct = st.slider(
        "穴の最低的中確率（%）",
        min_value=0.0,
        max_value=2.0,
        value=float(saved.get("longshot_min_prob_pct", 0.30)),
        step=0.05,
        help="原則、この確率未満の超低確率買い目は穴から除外します。",
    )
    value_bias = st.slider(
        "💰 妙味重視度（回収率志向）", 0.0, 1.0, float(saved.get("value_bias", 0.0)), 0.1,
        help=(
            "0は従来通り本線を厚めに配分。上げるほど「本線・抑え・穴」という"
            "カテゴリの序列より期待値（市場オッズに対するAIの優位性＝妙味）を"
            "重視した配分になります。的中率より回収率を狙うなら高めに。"
        ),
    )

    st.divider()
    _style_options = ["バランス", "展示重視"]
    _saved_style = saved.get("prediction_style", "バランス")
    prediction_style = st.selectbox(
        "🧭 予想スタイル",
        _style_options,
        index=_style_options.index(_saved_style) if _saved_style in _style_options else 0,
    )

    if prediction_style == "展示重視":
        default_display = 0.42
    else:
        default_display = 0.32

    display_weight = st.slider(
        "展示情報の反映度", 0.0, 0.8, default_display, 0.02,
        key=f"display_weight_{prediction_style}",
    )
    weather_weight = st.slider(
        "天候（風・波）の反映度", 0.0, 0.5, 0.10, 0.02,
        key=f"weather_weight_{prediction_style}",
        help="風速5m/s以上または波高3cm以上の荒れ水面で、アウトコース（5・6号艇）を不利側に補正します。",
    )
    venue_course_weight = st.slider(
        "場のコース特性（逃げ率・決まり手）の反映度", 0.0, 0.5, 0.12, 0.02,
        key=f"venue_course_weight_{prediction_style}",
        help="選手個人の実績ではなく、その場自体が「インが強いか」「まくりが決まりやすいか」という特性をBOAT RACE公式データから反映します。",
    )
    hedge_enabled = st.toggle(
        "🛟 本命艇の保険買い目", value=True,
        key=f"hedge_enabled_{prediction_style}",
        help="本命買い目には毎回、本命艇（多くは1号艇）がどこかしらの着順で含まれがちです。展示・モーター/ボート・今節成績を総合して本命艇に不安要素が多いと判定された場合、穴の1点を本命艇を含まない組み合わせに差し替え、「本命艇が飛ぶ」リスクに備えます。",
    )

    st.divider()
    if st.button("💾 この設定を保存（次回起動時も復元）"):
        new_settings = {
            "main_n": int(main_n),
            "cover_n": int(cover_n),
            "hole_n": int(hole_n),
            "total_budget": int(total_budget),
            "min_bet": int(min_bet),
            "longshot_min_prob_pct": float(longshot_min_prob_pct),
            "value_bias": float(value_bias),
            "prediction_style": prediction_style,
        }
        if save_settings(new_settings):
            st.session_state["app_settings"] = new_settings
            st.success("設定を保存しました。アプリを再起動しても復元されます。")
        else:
            st.error("設定の保存に失敗しました。")


with tab4:
    st.subheader("📊 予想検証")
    results_df = load_results()
    vm = validation_metrics(results_df)

    if vm["races"] == 0:
        st.info("まだ検証データがありません。AI予想後に実着順と払戻を登録すると、ここへ蓄積されます。")
    else:
        m1, m2 = st.columns(2)
        with m1:
            st.metric("検証レース数", f"{vm['races']}R")
            st.metric(
                "1着トップ予想 的中率",
                f"{vm['first_hit_rate']*100:.1f}%" if pd.notna(vm["first_hit_rate"]) else "-"
            )
            st.metric(
                "購入買い目 的中率",
                f"{vm['ticket_hit_rate']*100:.1f}%" if pd.notna(vm["ticket_hit_rate"]) else "-"
            )
        with m2:
            st.metric("累計収支", f"{vm['profit']:+,}円")
            st.metric(
                "回収率",
                f"{vm['roi']*100:.1f}%" if pd.notna(vm["roi"]) else "-"
            )
            st.metric(
                "簡易Brier score",
                f"{vm['brier_first']:.3f}" if pd.notna(vm["brier_first"]) else "-"
            )

        st.caption("Brier score は小さいほど、予測確率と実際の結果のズレが小さい指標です。")

        st.markdown("#### 1着予測の確率校正")
        cal = calibration_table(results_df)
        st.dataframe(cal, use_container_width=True, hide_index=True)

        st.markdown("#### 保存済みレース")
        show_cols = [
            "race_date","venue","race_no","trifecta_actual","p1_lane","p1_prob",
            "total_stake","payout","profit","roi"
        ]
        show = results_df[show_cols].copy()
        show = show.rename(columns={
            "race_date":"日付",
            "venue":"場",
            "race_no":"R",
            "trifecta_actual":"実3連単",
            "p1_lane":"AI本命",
            "p1_prob":"本命確率",
            "total_stake":"購入",
            "payout":"払戻",
            "profit":"収支",
            "roi":"回収倍率",
        })
        st.dataframe(show.sort_values(["日付","場","R"], ascending=False), use_container_width=True, hide_index=True)

        csv_export_results = results_df.copy()
        for _c in ("trifecta_actual", "top_ticket"):
            if _c in csv_export_results.columns:
                # combo表記("3-1-4"等)をExcelが日付だと誤解釈するのを防ぐ
                csv_export_results[_c] = "'" + csv_export_results[_c].astype(str)

        csv_export_results = csv_export_results.rename(columns={
            "saved_at": "保存日時",
            "race_date": "日付",
            "venue": "場",
            "race_no": "R",
            "race_key": "レースキー",
            "first_actual": "実1着",
            "second_actual": "実2着",
            "third_actual": "実3着",
            "trifecta_actual": "実3連単",
            "p1_lane": "AI本命艇",
            "p1_prob": "本命確率",
            "top_ticket": "本命買い目",
            "top_ticket_prob": "本命買い目確率",
            "top_ticket_odds": "本命買い目オッズ",
            "top_ticket_stake": "本命買い目購入額",
            "total_stake": "購入合計",
            "payout": "払戻",
            "profit": "収支",
            "roi": "回収倍率",
            "hit_top_ticket": "本命的中",
            "hit_any_ticket": "いずれか的中",
            "predicted_first_hit": "1着予想的中",
            "tickets_json": "買い目詳細",
            "lane_probs_json": "6艇予測詳細",
        })

        csv_results = csv_export_results.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 検証履歴CSV保存",
            csv_results,
            file_name="prediction_results.csv",
            mime="text/csv",
        )

        # -------------------------------------------------
        # 研究用：補正を1段ずつ足したときの長期成績比較
        # -------------------------------------------------
        research_rows = []
        for _, _rr in results_df.iterrows():
            raw = _rr.get("lane_probs_json", "")
            if not raw or pd.isna(raw):
                continue

            try:
                parsed = json.loads(raw)
            except Exception:
                continue

            if not isinstance(parsed, dict):
                continue

            variants_saved = parsed.get("research", {})
            if not isinstance(variants_saved, dict) or not variants_saved:
                continue

            try:
                actual_first = int(_rr.get("first_actual"))
            except Exception:
                continue

            for label, lanes in variants_saved.items():
                if not isinstance(lanes, list) or not lanes:
                    continue

                probs = {}
                for item in lanes:
                    try:
                        lane_no = int(item.get("lane"))
                        prob = float(item.get("p_first"))
                    except Exception:
                        continue
                    if 1 <= lane_no <= 6 and np.isfinite(prob):
                        probs[lane_no] = prob

                if len(probs) < 2:
                    continue

                top_lane = max(probs, key=probs.get)
                actual_prob = probs.get(actual_first, np.nan)
                brier6 = np.mean([
                    (probs.get(lane, 0.0) - (1.0 if lane == actual_first else 0.0)) ** 2
                    for lane in range(1, 7)
                ])

                research_rows.append({
                    "方式": label,
                    "本命的中": int(top_lane == actual_first),
                    "6艇Brier": float(brier6),
                    "実1着確率": actual_prob,
                })

        if research_rows:
            research_df = pd.DataFrame(research_rows)
            research_summary = (
                research_df.groupby("方式", sort=False)
                .agg(
                    検証R=("本命的中", "size"),
                    本命的中率=("本命的中", "mean"),
                    六艇Brier=("6艇Brier", "mean"),
                    実1着平均確率=("実1着確率", "mean"),
                )
                .reset_index()
            )
            research_summary["本命的中率"] = (research_summary["本命的中率"] * 100).round(1)
            research_summary["六艇Brier"] = research_summary["六艇Brier"].round(4)
            research_summary["実1着平均確率"] = (research_summary["実1着平均確率"] * 100).round(1)

            st.markdown("#### 🧪 研究用・補正別の長期比較")
            st.caption(
                "本番予想は『現行全部入り』のまま固定。ここは各補正を段階的に足した研究結果だけを比較します。"
                "6艇Brierは小さいほど良好です。"
            )
            st.dataframe(
                research_summary,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "本命的中率": st.column_config.NumberColumn(format="%.1f%%"),
                    "実1着平均確率": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )
        else:
            st.info(
                "🧪 補正別比較データはまだありません。研究比較機能追加後に保存したレースから自動で蓄積されます。"
            )

        st.markdown("#### 🔍 1レース詳細比較")
        st.caption("保存済みレースを選ぶと、AI予想（6艇の確率・買い目）と実際の結果を並べて確認できます。")

        detail_df = results_df.sort_values(["race_date", "venue", "race_no"], ascending=False).reset_index(drop=True)
        detail_labels = [
            f"{r['race_date']} {r['venue']} {int(r['race_no'])}R（実{r['trifecta_actual']}）"
            for _, r in detail_df.iterrows()
        ]

        if detail_labels:
            sel_idx = st.selectbox(
                "レースを選択",
                range(len(detail_labels)),
                format_func=lambda i: detail_labels[i],
                key="detail_race_select",
            )
            sel = detail_df.iloc[sel_idx]

            actual_combo = str(sel["trifecta_actual"])
            actual_lanes = actual_combo.split("-") if "-" in actual_combo else []

            st.markdown(f"##### {sel['race_date']} {sel['venue']} {int(sel['race_no'])}R")

            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                st.metric("実際の3連単", actual_combo)
            with dc2:
                st.metric("購入合計", f"{int(sel.get('total_stake', 0) or 0):,}円")
            with dc3:
                payout_v = sel.get("payout", 0)
                profit_v = sel.get("profit", 0)
                st.metric("払戻 / 収支", f"{int(payout_v or 0):,}円", delta=f"{int(profit_v or 0):+,}円")

            lane_probs_raw = sel.get("lane_probs_json", "")
            try:
                lane_probs_parsed = json.loads(lane_probs_raw) if lane_probs_raw and pd.notna(lane_probs_raw) else []
            except Exception:
                lane_probs_parsed = []

            # 旧データはlist、新データは {final: [...], research: {...}} 形式。
            if isinstance(lane_probs_parsed, dict):
                lane_probs = lane_probs_parsed.get("final", [])
                detail_research = lane_probs_parsed.get("research", {})
            else:
                lane_probs = lane_probs_parsed
                detail_research = {}

            if lane_probs:
                st.markdown("###### 6艇の予測確率 vs 実際の着順")
                lp = pd.DataFrame(lane_probs)
                lp["lane"] = pd.to_numeric(lp["lane"], errors="coerce").astype("Int64")

                def _actual_rank(lane_no):
                    lane_s = str(lane_no)
                    if lane_s in actual_lanes:
                        return actual_lanes.index(lane_s) + 1
                    return None

                lp["実着順"] = lp["lane"].apply(_actual_rank)
                lp = lp.sort_values("p_first", ascending=False)

                view = lp.rename(columns={
                    "lane": "艇",
                    "racer_name": "選手",
                    "p_first": "AI1着予測確率",
                    "reason": "根拠",
                })
                view["AI1着予測確率"] = (pd.to_numeric(view["AI1着予測確率"], errors="coerce") * 100).round(1)
                view["実着順"] = view["実着順"].apply(lambda v: f"{v}着" if v else "着外/不明")

                st.dataframe(
                    view[["艇", "選手", "AI1着予測確率", "実着順", "根拠"]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "AI1着予測確率": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )
            else:
                st.info("このレースは6艇分の予測確率データが保存される前に記録されたため、詳細比較はできません。")

            if detail_research:
                rows = []
                try:
                    actual_first = int(sel.get("first_actual"))
                except Exception:
                    actual_first = None

                for label, lanes in detail_research.items():
                    if not isinstance(lanes, list) or not lanes:
                        continue
                    probs = {}
                    for item in lanes:
                        try:
                            probs[int(item.get("lane"))] = float(item.get("p_first"))
                        except Exception:
                            pass
                    if not probs:
                        continue
                    top_lane = max(probs, key=probs.get)
                    rows.append({
                        "方式": label,
                        "本命艇": top_lane,
                        "本命確率": probs[top_lane] * 100,
                        "実1着確率": probs.get(actual_first, np.nan) * 100 if actual_first else np.nan,
                        "本命的中": "○" if actual_first and top_lane == actual_first else "",
                    })

                if rows:
                    st.markdown("###### 🧪 このレースの補正別比較")
                    st.dataframe(
                        pd.DataFrame(rows),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "本命確率": st.column_config.NumberColumn(format="%.1f%%"),
                            "実1着確率": st.column_config.NumberColumn(format="%.1f%%"),
                        },
                    )

            tickets_raw = sel.get("tickets_json", "")
            try:
                tickets_saved = json.loads(tickets_raw) if tickets_raw and pd.notna(tickets_raw) else []
            except Exception:
                tickets_saved = []

            if tickets_saved:
                st.markdown("###### 購入した買い目 vs 結果")
                tv = pd.DataFrame(tickets_saved)
                tv["的中"] = tv["combo"].astype(str).apply(lambda c: "🎯 的中" if c == actual_combo else "")
                tv["prob"] = (pd.to_numeric(tv.get("prob"), errors="coerce") * 100).round(2)

                tv = tv.rename(columns={
                    "combo": "買い目", "group": "区分", "prob": "的中確率(%)",
                    "odds": "オッズ", "expected_return": "期待値", "stake": "購入額",
                })
                cols = [c for c in ["買い目", "区分", "的中確率(%)", "オッズ", "期待値", "購入額", "的中"] if c in tv.columns]
                st.dataframe(tv[cols], use_container_width=True, hide_index=True)
            else:
                st.caption("このレースは購入した買い目の記録がありません。")


with tab1:
    d = st.date_input("日付", value=date.today())

    st.session_state.setdefault("selected_jcd", "01")

    st.markdown("### 🏟️ 会場を選択")
    try:
        schedule = cached_fetch_schedule(d.strftime("%Y%m%d"))
        schedule_by_jcd = {
            str(row["jcd"]): row for row in schedule.to_dict("records")
        }
    except Exception as e:
        st.caption("本日の開催状況を取得できませんでした（会場は手動で選べます）。")
        st.code(str(e))
        schedule_by_jcd = {}

    venue_codes = list(VENUES.keys())
    cols_per_row = 4

    venue_grid = st.container(key="venue_grid")

    for i in range(0, len(venue_codes), cols_per_row):
        row_codes = venue_codes[i:i + cols_per_row]
        cols = venue_grid.columns(len(row_codes))

        for col, code in zip(cols, row_codes):
            info = schedule_by_jcd.get(code, {})
            holding = bool(info.get("holding"))
            day_label = safe_name(info.get("day_label", ""))
            status = safe_name(info.get("status", ""))
            is_selected = st.session_state["selected_jcd"] == code

            marker = "🟢" if holding else "▫️"
            label = f"{marker} {VENUES[code]}"

            with col:
                if st.button(
                    label,
                    key=f"venue_btn_{code}",
                    use_container_width=True,
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state["selected_jcd"] = code
                    st.rerun()

                if holding:
                    st.caption(" / ".join(t for t in (day_label, status) if t) or "開催中")
                else:
                    st.caption("休み")

    jcd = st.session_state["selected_jcd"]
    st.info(f"選択中の会場：{jcd} {VENUES[jcd]}")

    c2a, c2b = st.columns(2)
    with c2a:
        rno = st.selectbox("レース", range(1, 13), index=11)
    with c2b:
        auto_odds = st.toggle("3連単オッズも取得", value=True)

    ctx = race_key(d, jcd, rno)

    if st.session_state.get("race_context") not in (None, ctx):
        st.info("日付・場・Rを変更しました。『公式データを取得』で新しいレースに切り替わります。")

    if st.button("📡 BOAT RACE公式データを取得", type="primary"):
        try:
            with st.spinner("公式ページを読み込み中…"):
                # 手動で「取得」を押したときは、展示タイムが後から公開
                # されたようなケースに対応するため、必ず最新のデータを
                # 取りに行く（キャッシュを一度クリアしてから取得）。
                # 復元専用の静かな自動取得（下のブロック）はキャッシュを
                # そのまま使うので、通信頻度は増えない。
                cached_fetch_race.clear()
                cached_fetch_odds.clear()
                race = cached_fetch_race(d.strftime("%Y%m%d"), jcd, rno)
                odds = cached_fetch_odds(d.strftime("%Y%m%d"), jcd, rno) if auto_odds else None
            st.session_state["race"] = race
            st.session_state["odds"] = odds
            st.session_state["race_context"] = ctx
            st.session_state.pop("result", None)
            # session_stateはブラウザ側の接続が切れると失われることがある
            # （スマホでバックグラウンド化した際など）。URLのクエリ
            # パラメータは接続が切れても残るため、「このレースは取得済み」
            # の目印として使い、再接続時にキャッシュから静かに復元する。
            st.query_params["fetched_ctx"] = ctx
            st.success("取得しました。下の内容を確認してAI予想へ。")
        except Exception as e:
            st.error("自動取得できませんでした。手動入力も利用できます。")
            st.code(str(e))

    race = st.session_state.get("race") if st.session_state.get("race_context") == ctx else None

    if race is None and st.query_params.get("fetched_ctx") == ctx:
        # 接続が切れてsession_stateが失われたケース。以前に取得済みの
        # 目印があるので、キャッシュから静かに復元を試みる
        # （キャッシュが生きていれば通信は発生せず一瞬で戻る）。
        try:
            with st.spinner("接続が切れたため復元しています…"):
                race = cached_fetch_race(d.strftime("%Y%m%d"), jcd, rno)
                odds = cached_fetch_odds(d.strftime("%Y%m%d"), jcd, rno) if auto_odds else None
            st.session_state["race"] = race
            st.session_state["odds"] = odds
            st.session_state["race_context"] = ctx
            st.info("接続切れから公式データを復元しました。")
        except Exception:
            race = None

    if race is None:
        st.info("公式データ取得を押すか、手動入力を作成してください。")
        if st.button("✍️ このレースの手動入力を作る"):
            race = pd.DataFrame({
                "date":[d.isoformat()]*6,"venue":[VENUES[jcd]]*6,"race_no":[rno]*6,"lane":range(1,7),
                "racer_name":[""]*6,"racer_class":[""]*6,"racer_win_rate":[np.nan]*6,"local_win_rate":[np.nan]*6,
                "motor_2ren":[np.nan]*6,"boat_2ren":[np.nan]*6,"avg_st":[0.16]*6,
                "exhibition_time":[np.nan]*6,"exhibition_st":[np.nan]*6,"weight":[np.nan]*6,"tilt":[np.nan]*6,
                "wind_speed":[np.nan]*6,"wave_height":[np.nan]*6,"temperature":[np.nan]*6,
            })
            st.session_state["race"] = race
            st.session_state["race_context"] = ctx
            st.session_state["odds"] = None
            st.rerun()

    if race is not None:
        race = ensure_columns(race, {
            "lane":np.nan,"racer_name":"","racer_class":"","racer_win_rate":np.nan,"local_win_rate":np.nan,
            "motor_2ren":np.nan,"boat_2ren":np.nan,"avg_st":np.nan,
            "exhibition_time":np.nan,"exhibition_st":np.nan,"weight":np.nan,"tilt":np.nan,
            "wind_speed":np.nan,"wave_height":np.nan,"temperature":np.nan,
        }).sort_values("lane").reset_index(drop=True)
        st.markdown("### 📊 今節成績")

        meet_cols = [
            "lane",
            "racer_name",
            "current_meet_avg_finish",
            "current_meet_top2_rate",
            "current_meet_avg_st",
            "current_meet_races",
        ]

        existing_meet_cols = [
            c for c in meet_cols
            if c in race.columns
        ]

        meet_display = race[existing_meet_cols].copy()

        meet_display = meet_display.rename(columns={
            "lane": "艇",
            "racer_name": "選手",
            "current_meet_avg_finish": "今節平均着順",
            "current_meet_top2_rate": "今節2連対率",
            "current_meet_avg_st": "今節平均ST",
            "current_meet_races": "今節走数",
        })

        st.dataframe(
            meet_display,
            width="stretch",
            hide_index=True,
        )
        st.markdown("### 📐 コース適性")

        course_cols = [
            "lane",
            "racer_name",
            "course_top3_rate",
            "course_avg_st",
            "course_start_rank",
        ]

        existing_course_cols = [
            c for c in course_cols
            if c in race.columns
        ]

        course_display = race[existing_course_cols].copy()

        course_display = course_display.rename(columns={
            "lane": "艇",
            "racer_name": "選手",
            "course_top3_rate": "コース3連対率",
            "course_avg_st": "コース平均ST",
            "course_start_rank": "コースST順位",
        })

        st.dataframe(
            course_display,
            width="stretch",
            hide_index=True,
        )
        st.markdown("### ① 選手・機力データ")
        basic_cols = ["lane","racer_name","racer_class","racer_win_rate","local_win_rate","motor_2ren","boat_2ren","avg_st"]
        basic = st.data_editor(
            race[basic_cols], use_container_width=True, hide_index=True, num_rows="fixed", key=f"basic_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "racer_name":st.column_config.TextColumn("選手"),
                "racer_class":st.column_config.SelectboxColumn("級別", options=["", "A1", "A2", "B1", "B2"]),
                "racer_win_rate":st.column_config.NumberColumn("全国勝率", format="%.2f"),
                "local_win_rate":st.column_config.NumberColumn("当地勝率", format="%.2f"),
                "motor_2ren":st.column_config.NumberColumn("モーター2連率", format="%.2f"),
                "boat_2ren":st.column_config.NumberColumn("ボート2連率", format="%.2f"),
                "avg_st":st.column_config.NumberColumn("平均ST", format="%.3f"),
            })
        miss = missing_summary(basic)
        if miss:
            st.warning("未取得・不足あり：" + " / ".join(miss))
        else:
            st.success("選手・機力の主要データは6艇分そろっています。")

        st.markdown("### ② 展示・直前データ")
        expo_cols = ["lane","exhibition_time","exhibition_st","weight","tilt","wind_speed","wave_height","temperature"]
        expo = st.data_editor(
            race[expo_cols], use_container_width=True, hide_index=True, num_rows="fixed", key=f"expo_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "exhibition_time":st.column_config.NumberColumn("展示タイム", format="%.2f"),
                "exhibition_st":st.column_config.NumberColumn("展示ST", format="%.2f"),
                "weight":st.column_config.NumberColumn("体重kg", format="%.1f"),
                "tilt":st.column_config.NumberColumn("チルト", format="%.1f"),
                "wind_speed":st.column_config.NumberColumn("風速m/s", format="%.1f"),
                "wave_height":st.column_config.NumberColumn("波高cm", format="%.1f"),
                "temperature":st.column_config.NumberColumn("気温℃", format="%.1f"),
            })
        n_display = pd.to_numeric(expo["exhibition_time"], errors="coerce").notna().sum()
        if n_display == 6:
            st.success("展示タイム：6艇分取得済み")
        elif n_display:
            st.warning(f"展示タイム：{n_display}/6艇")
        else:
            st.info("展示タイム未取得。展示前なら正常です。")

        edited = race.copy()

        for c in basic_cols:
            edited[c] = basic[c].to_numpy()
        for c in expo_cols:
            if c != "lane":
                edited[c] = expo[c].to_numpy()

        st.markdown("### ③ オリジナル展示")
        st.caption("直線・まわり足・1周タイム等を独自公開している場だけ入力。データがない場は空欄のままでOKです。")

        _orig_auto_cols = ["original_straight", "original_turn", "original_lap"]

        if OCR_AVAILABLE:
            with st.expander("📷 画像から自動入力（オリジナル展示のスクリーンショット）", expanded=False):
                st.caption(
                    "オリジナル展示のページのスクリーンショットをアップロードすると、"
                    "一周・まわり足・直線のタイムを自動で読み取って下の表に仮入力します。"
                    "OCR（文字認識）による読み取りのため誤りが混ざることがあります。"
                    "反映後は必ず下の表で数値を確認・修正してください。"
                )
                ocr_file = st.file_uploader(
                    "画像を選択", type=["png", "jpg", "jpeg"], key=f"orig_upload_{ctx}",
                )
                if ocr_file is not None and st.button("この画像から読み取る", key=f"orig_ocr_btn_{ctx}"):
                    with st.spinner("画像を解析中…"):
                        ocr_df = extract_original_exhibition(ocr_file.getvalue())
                    if ocr_df is None or ocr_df.empty:
                        st.warning(
                            "表を読み取れませんでした。表全体がはっきり写っている画像か、"
                            "拡大・トリミングして再度お試しください。"
                        )
                    else:
                        st.session_state[f"orig_ocr_data_{ctx}"] = ocr_df
                        st.session_state[f"orig_ver_{ctx}"] = (
                            st.session_state.get(f"orig_ver_{ctx}", 0) + 1
                        )
                        st.success("読み取りました。下の表に仮入力しています。数値を確認してください。")
                        st.rerun()

        orig = pd.DataFrame({"lane": range(1, 7)})
        for c in _orig_auto_cols:
            orig[c] = pd.to_numeric(race[c], errors="coerce") if c in race.columns else np.nan

        _ocr_df = st.session_state.get(f"orig_ocr_data_{ctx}")
        if _ocr_df is not None:
            orig = orig.drop(columns=_orig_auto_cols).merge(_ocr_df, on="lane", how="left")

        _orig_source = safe_name(race["original_exhibition_source"].dropna().iloc[0]) if (
            "original_exhibition_source" in race.columns
            and race["original_exhibition_source"].astype(str).str.strip().any()
        ) else ""

        _orig_ver = st.session_state.get(f"orig_ver_{ctx}", 0)
        orig = st.data_editor(
            orig, use_container_width=True, hide_index=True, num_rows="fixed", key=f"orig_{ctx}_{_orig_ver}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "original_straight":st.column_config.NumberColumn("直線", format="%.2f"),
                "original_turn":st.column_config.NumberColumn("まわり足", format="%.2f"),
                "original_lap":st.column_config.NumberColumn("1周", format="%.2f"),
            })
        if orig[["original_straight","original_turn","original_lap"]].apply(pd.to_numeric, errors="coerce").notna().any().any():
            if _orig_source:
                st.success(f"オリジナル展示を自動取得済み（{_orig_source}）。AI補正に使用します。")
            else:
                st.success("オリジナル展示をAI補正に使用します。")
        else:
            st.info("オリジナル展示：未取得・未入力。通常展示・基礎データ中心で予想します。")

        work = edited.merge(orig, on="lane", how="left")
        work["date"] = d.isoformat()
        work["venue"] = VENUES[jcd]
        work["race_no"] = rno

        st.markdown("### ④ 3連単オッズ")
        odds = st.session_state.get("odds")
        if odds is not None and len(odds):
            st.success(f"3連単オッズ {len(odds)}通りを自動取得")
        else:
            st.info("オッズなしでも確率予想は可能。期待値を出す場合はCSVを追加してください。")
            odds_up = st.file_uploader("オッズCSV（任意）", type="csv", key=f"oddsfile_{ctx}", help="combo,odds の2列。例：1-2-3,12.5")
            if odds_up:
                odds = pd.read_csv(odds_up)

        if odds_tracking_available():
            with st.expander("📈 オッズの時系列追跡", expanded=False):
                st.caption("登録すると、締切まで数分おきに自動でオッズを記録し、動きを確認できるようになります。")
                if st.button("この開催レースの追跡を開始", key=f"watch_{ctx}"):
                    ok, msg = add_to_odds_watchlist(d.strftime("%Y%m%d"), jcd, rno)
                    if ok:
                        st.success(msg)
                        saved_now, save_msg = save_odds_snapshot_now(
                            d.strftime("%Y%m%d"), jcd, rno, odds
                        )
                        if saved_now:
                            st.success(f"⚡ {save_msg}")
                        else:
                            st.warning(
                                f"{save_msg} 定期追跡は開始済みなので、"
                                "次回のGitHub Actionsでも保存を試みます。"
                            )
                    else:
                        st.error(msg)

                hist = load_odds_history(d.strftime("%Y%m%d"), jcd, rno)
                if len(hist):
                    st.caption(f"記録済み：{hist['fetched_at'].nunique()}時点分")
                    pivot = hist.pivot_table(index="fetched_at", columns="combo", values="odds")
                    # 動きが大きい（下落幅が大きい）上位5点だけをグラフ表示
                    if len(pivot.columns) and len(pivot) >= 2:
                        change = pivot.iloc[-1] - pivot.iloc[0]
                        top_movers = change.sort_values().head(5).index.tolist()
                        st.line_chart(pivot[top_movers])
                        st.caption("下落幅が大きい上位5買い目のオッズ推移（人気が集まっている＝妙味が薄れつつある買い目）")
                    else:
                        st.info("まだ記録が1時点分しかありません。もう少し待ってから確認してください。")
                else:
                    st.info("まだ追跡記録がありません。「追跡を開始」を押してから数分待ってください。")

        st.divider()
        if st.button("🤖 AI最終予想", type="primary"):
            try:
                with st.spinner("AI解析中…"):
                    model = train(history)
                    pre = work.copy()
                    pre["exhibition_time"] = np.nan
                    pre["original_straight"] = np.nan
                    pre["original_turn"] = np.nan
                    pre["original_lap"] = np.nan
                    pre["exhibition_st"] = np.nan
                    before = predict(model, pre, display_weight=0, weather_weight=0, venue_course_weight=0)
                    final = predict(model, work, display_weight=display_weight, weather_weight=weather_weight, venue_course_weight=venue_course_weight)

                    # 研究用の比較は別計算。final（本番予想）は一切変更しない。
                    research_variants = research_prediction_variants(
                        model,
                        work,
                        display_weight=display_weight,
                        weather_weight=weather_weight,
                        venue_course_weight=venue_course_weight,
                    )
                    tri = trifecta(final)

                    favorite_lane, risk_score, risk_reasons = assess_favorite_risk(work, final)
                    hedge_lane = favorite_lane if (hedge_enabled and risk_score >= 2) else None

                    tickets = rank_tickets(
                        tri,
                        odds,
                        main_n=int(main_n),
                        cover_n=int(cover_n),
                        longshot_n=int(hole_n),
                        longshot_min_prob=float(longshot_min_prob_pct) / 100.0,
                        hedge_lane=hedge_lane,
                    )
                    tickets = allocate_stakes_smart(
                        tickets,
                        budget=int(total_budget),
                        unit=100,
                        min_bet=int(min_bet),
                        max_longshot_share=0.15,
                        max_ticket_share=0.35,
                        value_bias=float(value_bias),
                    )
                    st.session_state["result"] = {
                        "context": ctx,
                        "before": before,
                        "final": final,
                        "tickets": tickets,
                        "work": work,
                        "hedge_lane": hedge_lane,
                        "risk_reasons": risk_reasons,
                        "research_variants": research_variants,
                    }
            except Exception as e:
                st.error("AI予想でエラーが発生しました。")
                st.code(str(e))

        result = st.session_state.get("result")
        if result and result.get("context") == ctx:
            before, final, tickets, work_result = result["before"], result["final"], result["tickets"], result["work"]

            if "stake" not in tickets.columns:
                tickets = allocate_stakes_smart(
                    tickets,
                    budget=int(total_budget),
                    unit=100,
                    min_bet=int(min_bet),
                    max_longshot_share=0.15,
                    max_ticket_share=0.35,
                    value_bias=float(value_bias),
                )
                st.session_state["result"]["tickets"] = tickets

            tickets = tickets.copy()
            tickets["stake"] = pd.to_numeric(tickets["stake"], errors="coerce").fillna(0).astype(int)

            st.divider()
            st.subheader(f"{VENUES[jcd]} {rno}R AI最終予想")

            mc1, mc2 = st.columns(2)
            with mc1:
                st.metric("AI総合信頼度", confidence(final, work_result))
            with mc2:
                total_stake_metric = int(tickets["stake"].sum())
                st.metric("推奨購入総額", f"{total_stake_metric:,}円")

            snapshot = load_prediction_snapshot(ctx)
            if snapshot:
                kind_label = (
                    "過去レース・バックテスト"
                    if snapshot.get("snapshot_kind") == "backtest"
                    else "当日予想"
                )
                st.success(
                    "📌 検証用予想は固定済みです。"
                    f" 固定時刻: {snapshot.get('saved_at', '-')} / {kind_label}。"
                    "このあとAIを再計算しても、結果保存では固定済み予想を使います。"
                )
            else:
                if d < date.today():
                    st.warning(
                        "📌 このレースは過去レースです。ここで固定するとバックテスト扱いになります。"
                        "買い目・回収率は締切後データが混ざる可能性があるため参考値として扱ってください。"
                    )
                    snapshot_kind = "backtest"
                else:
                    st.warning(
                        "📌 レース前に『この予想を検証用に固定』を押してください。"
                        "固定後はレース終了後に再取得・再予想しても、検証成績はこの時点の予想で判定します。"
                    )
                    snapshot_kind = "same_day"

                if st.button(
                    "📌 この予想を検証用に固定",
                    key=f"lock_prediction_{ctx}",
                ):
                    try:
                        snap = save_prediction_snapshot(
                            race_key=ctx,
                            race_date=d.isoformat(),
                            venue=VENUES[jcd],
                            race_no=rno,
                            final=final,
                            tickets=tickets,
                            research_variants=st.session_state["result"].get(
                                "research_variants",
                                {},
                            ),
                            snapshot_kind=snapshot_kind,
                        )
                        st.success(
                            "検証用予想を固定しました。"
                            f" 固定時刻: {snap.get('saved_at', '-')}"
                        )

                        # 本番（当日・レース前）で予想を固定したら、
                        # 同じレースをオッズ追跡対象へ自動登録する。
                        # バックテストでは終了後オッズを追跡しても意味がないため登録しない。
                        if snapshot_kind == "same_day" and odds_tracking_available():
                            ok, msg = add_to_odds_watchlist(
                                d.strftime("%Y%m%d"),
                                jcd,
                                rno,
                            )
                            if ok:
                                st.success(
                                    "📈 オッズ自動追跡も開始しました。"
                                    " GitHub Actionsが数分おきに記録します。"
                                )
                                saved_now, save_msg = save_odds_snapshot_now(
                                    d.strftime("%Y%m%d"), jcd, rno, odds
                                )
                                if saved_now:
                                    st.success(f"⚡ {save_msg}")
                                else:
                                    st.warning(
                                        f"{save_msg} 定期追跡は開始済みなので、"
                                        "次回のGitHub Actionsでも保存を試みます。"
                                    )
                            else:
                                # 予想固定自体は成功済みなので、追跡登録の失敗だけを警告する。
                                st.warning(
                                    "予想の固定は成功しましたが、"
                                    f"オッズ追跡の開始に失敗しました。 {msg}"
                                )

                        st.rerun()
                    except Exception as e:
                        st.error("検証用予想の固定に失敗しました。")
                        st.code(str(e))

            merged = final.merge(before[["lane","p_first"]], on="lane", suffixes=("_after","_before"))
            merged["変化"] = merged["p_first_after"] - merged["p_first_before"]
            merged = merged.sort_values("p_first_after", ascending=False)

            research_variants = st.session_state["result"].get("research_variants", {})
            if research_variants:
                with st.expander("🧪 研究用：補正を1段ずつ比較", expanded=False):
                    st.caption(
                        "本番表示・買い目は『現行全部入り』の結果をそのまま使用しています。"
                        "この表は研究用で、予想結果には影響しません。"
                    )
                    rows = []
                    for label, variant_df in research_variants.items():
                        ranked = variant_df.sort_values("p_first", ascending=False).reset_index(drop=True)
                        if len(ranked) == 0:
                            continue
                        top = ranked.iloc[0]
                        rows.append({
                            "方式": label,
                            "本命艇": int(top["lane"]),
                            "本命確率": float(top["p_first"]) * 100,
                        })
                    if rows:
                        st.dataframe(
                            pd.DataFrame(rows),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "本命確率": st.column_config.NumberColumn(format="%.1f%%"),
                            },
                        )

            hedge_lane = st.session_state["result"].get("hedge_lane")
            risk_reasons = st.session_state["result"].get("risk_reasons", [])
            if hedge_lane is not None:
                st.warning(
                    f"🛟 {hedge_lane}号艇（本命）に不安要素あり（{' / '.join(risk_reasons)}）のため、"
                    f"穴の1点を{hedge_lane}号艇を含まない保険買い目に差し替えています。"
                )

            st.markdown("### 1着確率")
            for _, row in merged.iterrows():
                lane = int(row["lane"])
                nm = safe_name(row.get("racer_name", ""))

                st.markdown(
                    f"""<div class="ticket"><b>{lane}号艇 {nm}</b><br>
1着 <b>{row['p_first_after']*100:.1f}%</b>
<span class="small">展示前 {row['p_first_before']*100:.1f}% / {row['変化']*100:+.1f}pt</span><br>
<span class="small">{row['reason']}</span></div>""",
                    unsafe_allow_html=True
                )

            for group, emoji in [("本線","🔥"),("抑え","🛟"),("穴","💎")]:
                st.markdown(f"### {emoji} {group}")
                g = tickets[tickets["group"] == group]

                if len(g) == 0:
                    st.caption("なし")

                for _, row in g.iterrows():
                    oddtxt = f"{row['odds']:.1f}倍" if pd.notna(row.get("odds")) else "オッズ未取得"
                    evtxt = f" / 期待値 {row['expected_return']:.2f}" if pd.notna(row.get("expected_return")) else ""
                    stake = int(row.get("stake", 0) or 0)
                    stake_txt = f"{stake:,}円" if stake > 0 else "見送り"

                    st.markdown(
                        f"""<div class="ticket">
<b>{row['combo']}</b>　的中確率 <b>{row['prob']*100:.2f}%</b><br>
<span class="small">{oddtxt}{evtxt}</span><br>
<div class="money">💴 推奨 {stake_txt}</div>
<span class="small">{row.get('stake_reason', '')}</span>
</div>""",
                        unsafe_allow_html=True
                    )

            st.markdown("### 💴 購入配分")
            buy_view = tickets[["combo", "group", "stake"]].copy()
            buy_view = buy_view[buy_view["stake"] > 0]

            if len(buy_view):
                st.dataframe(
                    buy_view,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "combo":"買い目",
                        "group":"区分",
                        "stake":st.column_config.NumberColumn("購入額", format="%d円"),
                    }
                )
                st.success(f"購入合計：{int(buy_view['stake'].sum()):,}円")
            else:
                st.info("購入推奨額はありません。")

            csv_export = tickets.copy()
            if "combo" in csv_export.columns:
                # Excelは "3-1-4" のような買い目表記を日付だと誤解釈して
                # 例えば "2003/1/4" のように勝手に変換してしまうことがある。
                # 先頭に ' を付けるとExcel上ではテキスト扱いになり、
                # セルの見た目には出ない（数式バーにのみ残る）。
                csv_export["combo"] = "'" + csv_export["combo"].astype(str)

            csv_export = csv_export.rename(columns={
                "combo": "買い目",
                "prob": "的中確率",
                "odds": "オッズ",
                "expected_return": "期待値",
                "group": "区分",
                "stake": "購入額",
                "stake_reason": "配分理由",
            })

            csv = csv_export.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 買い目CSV保存",
                csv,
                file_name=f"{d}_{VENUES[jcd]}_{rno}R_tickets.csv",
                mime="text/csv"
            )

            st.divider()
            st.markdown("### ✅ レース結果を検証保存")

            if result_exists(ctx):
                st.success("このレースは検証履歴に保存済みです。再保存すると上書きします。")

            # 半自動検証：
            # 結果確定後に公式結果を1クリック取得し、固定予想に対する
            # 実受取額まで自動計算する。最終保存は人が確認して押す。
            if st.button("🏁 公式結果を自動取得", key=f"fetch_result_{ctx}"):
                try:
                    official_result = fetch_race_result(
                        d.strftime("%Y%m%d"),
                        jcd,
                        rno,
                    )

                    combo = official_result["trifecta"]
                    received, hit_stake = snapshot_payout_from_official(
                        ctx,
                        combo,
                        official_result["trifecta_payout_per_100"],
                    )

                    st.session_state[f"actual1_{ctx}"] = int(official_result["first"])
                    st.session_state[f"actual2_{ctx}"] = int(official_result["second"])
                    st.session_state[f"actual3_{ctx}"] = int(official_result["third"])
                    st.session_state[f"payout_{ctx}"] = int(received)
                    st.session_state[f"official_result_{ctx}"] = {
                        **official_result,
                        "received": int(received),
                        "hit_stake": int(hit_stake),
                    }

                    # 結果が確定したレースは追跡対象から外して、
                    # 不要な公式アクセスを止める。
                    deactivate_odds_watchlist(
                        d.strftime("%Y%m%d"),
                        jcd,
                        rno,
                    )
                    st.rerun()

                except Exception as e:
                    st.warning(
                        "公式結果をまだ取得できませんでした。"
                        "結果確定後にもう一度押すか、従来どおり手入力してください。"
                    )
                    st.caption(str(e))

            official_result_state = st.session_state.get(
                f"official_result_{ctx}"
            )
            if official_result_state:
                hit_text = (
                    f"的中購入額 {official_result_state['hit_stake']:,}円"
                    if official_result_state["hit_stake"] > 0
                    else "固定買い目は不的中"
                )
                st.success(
                    "🏁 公式結果取得済み："
                    f"{official_result_state['trifecta']} / "
                    f"3連単 {official_result_state['trifecta_payout_per_100']:,}円（100円あたり） / "
                    f"{hit_text} / "
                    f"実受取 {official_result_state['received']:,}円"
                )
                st.caption(
                    "着順と払戻受取額を下に自動入力しました。"
                    "内容を確認してから検証履歴へ保存してください。"
                )

            rc1, rc2, rc3 = st.columns(3)
            with rc1:
                actual_1 = st.selectbox("実1着", range(1,7), key=f"actual1_{ctx}")
            with rc2:
                actual_2 = st.selectbox("実2着", range(1,7), index=1, key=f"actual2_{ctx}")
            with rc3:
                actual_3 = st.selectbox("実3着", range(1,7), index=2, key=f"actual3_{ctx}")

            payout_input = st.number_input(
                "このレースの実払戻受取額（円）",
                min_value=0,
                max_value=10000000,
                value=0,
                step=100,
                key=f"payout_{ctx}",
                help="購入した買い目が外れなら0円。当たった場合は実際に受け取った合計払戻額を入力。",
            )

            if len({actual_1, actual_2, actual_3}) < 3:
                st.warning("1着・2着・3着は別々の艇を選んでください。")
            else:
                snapshot_for_result = load_prediction_snapshot(ctx)

                if snapshot_for_result is None:
                    st.error(
                        "⚠️ このレースは検証用予想が固定されていません。"
                        "レース終了後の再予想が混ざるのを防ぐため、結果保存は行いません。"
                    )
                elif st.button("💾 実結果を検証履歴へ保存", key=f"save_result_{ctx}"):
                    rec = save_race_result(
                        race_key=ctx,
                        race_date=d.isoformat(),
                        venue=VENUES[jcd],
                        race_no=rno,
                        final=final,
                        tickets=tickets,
                        first_actual=actual_1,
                        second_actual=actual_2,
                        third_actual=actual_3,
                        payout=int(payout_input),
                        research_variants=st.session_state["result"].get("research_variants", {}),
                        prefer_snapshot=True,
                    )
                    hit_text = "的中" if rec["hit_any_ticket"] else "不的中"
                    source_text = (
                        "固定予想で判定"
                        if rec.get("_used_snapshot")
                        else "現在予想で判定"
                    )
                    st.success(
                        f"保存しました：実結果 {rec['trifecta_actual']} / "
                        f"購入買い目 {hit_text} / 収支 {rec['profit']:+,}円 / "
                        f"{source_text}"
                    )

            st.warning("AI予想は確率推定であり、的中・利益を保証しません。オッズ変動、欠場・返還、展示と本番の進入差にも注意してください。")
