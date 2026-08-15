from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from official_fetcher import VENUES, fetch_official_race, fetch_odds3t
from prediction import train, predict, trifecta, rank_tickets, allocate_stakes, confidence
from comment_analyzer import analyze_comment

st.set_page_config(page_title="BOAT AI Mobile", page_icon="🚤", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:.8rem;padding-bottom:5rem}
.stButton>button{width:100%;min-height:3rem;border-radius:14px;font-weight:700}
.ticket{padding:.8rem 1rem;border:1px solid rgba(150,150,150,.28);border-radius:14px;margin:.45rem 0}
.small{opacity:.72;font-size:.86rem}
</style>
""", unsafe_allow_html=True)

st.title("🚤 BOAT AI Mobile")
st.caption("公式情報＋展示＋コメントを合わせて、3連単を『本線・抑え・穴』に整理")

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

tab1, tab2, tab3 = st.tabs(["🎯 予想", "🧠 学習データ", "⚙️ 設定"])

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
    main_n = st.number_input("🔥 本線 点数", 1, 10, 3)
    cover_n = st.number_input("🛟 抑え 点数", 0, 10, 3)
    hole_n = st.number_input("💎 穴 点数", 0, 10, 2)

    st.divider()
    total_budget = st.number_input(
        "💴 1レース予算",
        min_value=500,
        max_value=100000,
        value=2000,
        step=100,
    )
    min_bet = st.number_input(
        "1点あたり最低購入額",
        min_value=100,
        max_value=1000,
        value=100,
        step=100,
    )
    longshot_min_prob_pct = st.slider(
        "穴の最低的中確率（%）",
        min_value=0.0,
        max_value=2.0,
        value=0.30,
        step=0.05,
        help="原則、この確率未満の超低確率買い目は穴から除外します。",
    )

    display_weight = st.slider("展示情報の反映度", 0.0, 0.8, 0.32, 0.02)
    comment_weight = st.slider("選手コメントの反映度", 0.0, 0.6, 0.18, 0.02)

with tab1:
    c1, c2 = st.columns(2)
    with c1:
        d = st.date_input("日付", value=date.today())
        jcd = st.selectbox("競艇場", list(VENUES.keys()), format_func=lambda x: f"{x} {VENUES[x]}")
    with c2:
        rno = st.selectbox("レース", range(1, 13), index=11)
        auto_odds = st.toggle("3連単オッズも取得", value=True)

    ctx = race_key(d, jcd, rno)

    if st.session_state.get("race_context") not in (None, ctx):
        st.info("日付・場・Rを変更しました。『公式データを取得』で新しいレースに切り替わります。")

    if st.button("📡 BOAT RACE公式データを取得", type="primary"):
        try:
            with st.spinner("公式ページを読み込み中…"):
                race = fetch_official_race(d.strftime("%Y%m%d"), jcd, rno)
                odds = fetch_odds3t(d.strftime("%Y%m%d"), jcd, rno) if auto_odds else None
            st.session_state["race"] = race
            st.session_state["odds"] = odds
            st.session_state["race_context"] = ctx
            st.session_state.pop("result", None)
            st.success("取得しました。下の内容を確認してAI予想へ。")
        except Exception as e:
            st.error("自動取得できませんでした。手動入力も利用できます。")
            st.code(str(e))

    race = st.session_state.get("race") if st.session_state.get("race_context") == ctx else None

    if race is None:
        st.info("公式データ取得を押すか、手動入力を作成してください。")
        if st.button("✍️ このレースの手動入力を作る"):
            race = pd.DataFrame({
                "date":[d.isoformat()]*6,"venue":[VENUES[jcd]]*6,"race_no":[rno]*6,"lane":range(1,7),
                "racer_name":[""]*6,"racer_win_rate":[np.nan]*6,"local_win_rate":[np.nan]*6,
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
            "lane":np.nan,"racer_name":"","racer_win_rate":np.nan,"local_win_rate":np.nan,
            "motor_2ren":np.nan,"boat_2ren":np.nan,"avg_st":np.nan,
            "exhibition_time":np.nan,"exhibition_st":np.nan,"weight":np.nan,"tilt":np.nan,
            "wind_speed":np.nan,"wave_height":np.nan,"temperature":np.nan,
        }).sort_values("lane").reset_index(drop=True)

        st.markdown("### ① 選手・機力データ")
        basic_cols = ["lane","racer_name","racer_win_rate","local_win_rate","motor_2ren","boat_2ren","avg_st"]
        basic = st.data_editor(
            race[basic_cols], use_container_width=True, hide_index=True, num_rows="fixed", key=f"basic_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "racer_name":st.column_config.TextColumn("選手"),
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
        orig = pd.DataFrame({
            "lane":range(1,7),"original_straight":[np.nan]*6,"original_turn":[np.nan]*6,"original_lap":[np.nan]*6,
        })
        orig = st.data_editor(
            orig, use_container_width=True, hide_index=True, num_rows="fixed", key=f"orig_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "original_straight":st.column_config.NumberColumn("直線", format="%.2f"),
                "original_turn":st.column_config.NumberColumn("まわり足", format="%.2f"),
                "original_lap":st.column_config.NumberColumn("1周", format="%.2f"),
            })
        if orig[["original_straight","original_turn","original_lap"]].apply(pd.to_numeric, errors="coerce").notna().any().any():
            st.success("オリジナル展示をAI補正に使用します。")
        else:
            st.info("オリジナル展示：未入力。通常展示・基礎データ中心で予想します。")

        st.markdown("### ④ 選手コメント")
        comments = []
        for i in range(6):
            nm = safe_name(edited.iloc[i].get("racer_name", ""))
            txt = st.text_input(f"{i+1}号艇" + (f" {nm}" if nm else ""), key=f"comment_{ctx}_{i}", placeholder="例：伸びはいい。回った後も悪くない")
            comments.append(txt)
        with st.expander("コメント解析を見る"):
            any_comment = False
            for i, txt in enumerate(comments):
                if txt.strip():
                    any_comment = True
                    scores, _ = analyze_comment(txt)
                    st.write(f"**{i+1}号艇**", scores)
            if not any_comment:
                st.caption("コメント入力後、ここに解析結果が表示されます。")

        work = edited.merge(orig, on="lane", how="left")
        work["comment"] = comments
        work["date"] = d.isoformat()
        work["venue"] = VENUES[jcd]
        work["race_no"] = rno

        st.markdown("### ⑤ 3連単オッズ")
        odds = st.session_state.get("odds")
        if odds is not None and len(odds):
            st.success(f"3連単オッズ {len(odds)}通りを自動取得")
        else:
            st.info("オッズなしでも確率予想は可能。期待値を出す場合はCSVを追加してください。")
            odds_up = st.file_uploader("オッズCSV（任意）", type="csv", key=f"oddsfile_{ctx}", help="combo,odds の2列。例：1-2-3,12.5")
            if odds_up:
                odds = pd.read_csv(odds_up)

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
                    pre["comment"] = ""
                    before = predict(model, pre, display_weight=0, comment_weight=0)
                    final = predict(model, work, display_weight=display_weight, comment_weight=comment_weight)
                    tri = trifecta(final)
                    tickets = rank_tickets(
                        tri,
                        odds,
                        main_n=int(main_n),
                        cover_n=int(cover_n),
                        longshot_n=int(hole_n),
                        longshot_min_prob=float(longshot_min_prob_pct) / 100.0,
                    )
                    tickets = allocate_stakes(
                        tickets,
                        budget=int(total_budget),
                        unit=100,
                        min_bet=int(min_bet),
                    )
                    st.session_state["result"] = {
                        "context": ctx,
                        "before": before,
                        "final": final,
                        "tickets": tickets,
                        "work": work,
                    }
            except Exception as e:
                st.error("AI予想でエラーが発生しました。")
                st.code(str(e))

        result = st.session_state.get("result")
        if result and result.get("context") == ctx:
            before, final, tickets, work_result = result["before"], result["final"], result["tickets"], result["work"]

            # 旧バージョンのsession_stateが残っていてstake列が無い場合も自動復旧
            if "stake" not in tickets.columns:
                tickets = allocate_stakes(
                    tickets,
                    budget=int(total_budget),
                    unit=100,
                    min_bet=int(min_bet),
                )
                st.session_state["result"]["tickets"] = tickets

            st.divider()
            st.subheader(f"{VENUES[jcd]} {rno}R AI最終予想")
            mc1,mc2=st.columns(2)
            with mc1:
                st.metric("AI総合信頼度", confidence(final, work_result))
            with mc2:
                st.metric("推奨購入総額",f"{int(tickets['stake'].sum()):,}円")
            merged = final.merge(before[["lane","p_first"]], on="lane", suffixes=("_after","_before"))
            merged["変化"] = merged["p_first_after"] - merged["p_first_before"]
            merged = merged.sort_values("p_first_after", ascending=False)
            st.markdown("### 1着確率")
            for _, row in merged.iterrows():
                lane = int(row["lane"]); nm = safe_name(row.get("racer_name", ""))
                st.markdown(f"""<div class="ticket"><b>{lane}号艇 {nm}</b><br>1着 <b>{row['p_first_after']*100:.1f}%</b> <span class="small">展示前 {row['p_first_before']*100:.1f}% / {row['変化']*100:+.1f}pt</span><br><span class="small">{row['reason']}</span></div>""", unsafe_allow_html=True)
            for group, emoji in [("本線","🔥"),("抑え","🛟"),("穴","💎")]:
                st.markdown(f"### {emoji} {group}")
                g = tickets[tickets["group"] == group]
                if len(g) == 0:
                    st.caption("なし")
                for _, row in g.iterrows():
                    oddtxt = f"{row['odds']:.1f}倍" if pd.notna(row.get("odds")) else "オッズ未取得"
                    evtxt = f" / 期待値 {row['expected_return']:.2f}" if pd.notna(row.get("expected_return")) else ""
                    st.markdown(f"""<div class="ticket"><b>{row['combo']}</b>　的中確率 <b>{row['prob']*100:.2f}%</b><br><span class="small">{oddtxt}{evtxt}</span></div>""", unsafe_allow_html=True)
            csv = tickets.to_csv(index=False).encode("utf-8-sig")
            st.download_button("📥 買い目CSV保存", csv, file_name=f"{d}_{VENUES[jcd]}_{rno}R_tickets.csv", mime="text/csv")
            st.warning("AI予想は確率推定であり、的中・利益を保証しません。オッズ変動、欠場・返還、展示と本番の進入差にも注意してください。")
