
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st
from official_fetcher import VENUES, fetch_official_race, fetch_odds3t
from prediction import train, predict, trifecta, rank_tickets, confidence
from comment_analyzer import analyze_comment

st.set_page_config(page_title="BOAT AI Mobile v2",page_icon="🚤",layout="centered",initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:1rem;padding-bottom:5rem}
div[data-testid="stMetric"]{border:1px solid rgba(150,150,150,.25);padding:.65rem;border-radius:14px}
.stButton>button{width:100%;height:3rem;border-radius:14px;font-weight:700}
.ticket{padding:.75rem 1rem;border:1px solid rgba(150,150,150,.28);border-radius:14px;margin:.4rem 0}
.small{opacity:.72;font-size:.85rem}
</style>
""",unsafe_allow_html=True)

st.title("🚤 BOAT AI Mobile v2")
st.caption("公式情報＋展示＋コメントを合わせて、3連単を「本線・抑え・穴」に整理するスマホ向け試作版")

@st.cache_data
def load_demo_history():
    return pd.read_csv(Path(__file__).parent/"sample_history.csv")

st.session_state.setdefault("race",None)
st.session_state.setdefault("odds",None)

tab1,tab2,tab3=st.tabs(["🎯 予想","🧠 学習データ","⚙️ 設定"])

with tab2:
    st.subheader("学習データ")
    hist_up=st.file_uploader("過去成績CSV",type="csv",key="hist")
    history=pd.read_csv(hist_up) if hist_up else load_demo_history()
    st.caption(f"{len(history):,}行を使用。未アップロード時は動作確認用の合成データです。")
    st.dataframe(history.head(12),use_container_width=True,hide_index=True)
    st.warning("実戦評価には必ず実データで時系列バックテストをしてください。サンプル学習データの予想は収益性を示しません。")

with tab3:
    st.subheader("買い目設定")
    main_n=st.number_input("本線 点数",1,10,3)
    cover_n=st.number_input("抑え 点数",0,10,3)
    hole_n=st.number_input("穴 点数",0,10,2)
    display_weight=st.slider("展示情報の反映度",0.0,0.8,0.32,0.02)
    comment_weight=st.slider("選手コメントの反映度",0.0,0.6,0.18,0.02)
    st.caption("コメント補正はルールベースの試作です。将来は過去コメントを蓄積して学習型に置き換えられます。")

with tab1:
    c1,c2=st.columns(2)
    with c1:
        d=st.date_input("日付",value=date.today())
        jcd=st.selectbox("競艇場",list(VENUES.keys()),format_func=lambda x:f"{x} {VENUES[x]}")
    with c2:
        rno=st.selectbox("レース",range(1,13),index=11)
        auto_odds=st.toggle("3連単オッズも取得",value=True)

    if st.button("📡 BOAT RACE公式データを取得",type="primary"):
        ds=d.strftime("%Y%m%d")
        try:
            with st.spinner("公式ページを読み込み中…"):
                st.session_state["race"]=fetch_official_race(ds,jcd,rno)
                st.session_state["odds"]=fetch_odds3t(ds,jcd,rno) if auto_odds else None
            st.success("取得しました。下の表を確認・修正してAI予想へ。")
        except Exception as e:
            st.error("自動取得できませんでした。サイト側変更・未公開・通信制限の可能性があります。下の手動入力を使えます。")
            st.code(str(e))

    race=st.session_state.get("race")
    if race is None:
        st.info("まず公式データ取得を押すか、下の「手動入力を作る」を使ってください。")
        if st.button("✍️ 手動入力を作る"):
            race=pd.DataFrame({
                "date":[d.isoformat()]*6,"venue":[VENUES[jcd]]*6,"race_no":[rno]*6,"lane":range(1,7),
                "racer_name":[""]*6,"racer_win_rate":[np.nan]*6,"local_win_rate":[np.nan]*6,
                "motor_2ren":[np.nan]*6,"boat_2ren":[np.nan]*6,"avg_st":[.16]*6,
                "exhibition_time":[np.nan]*6,"exhibition_st":[np.nan]*6,"weight":[np.nan]*6,"tilt":[np.nan]*6,
                "wind_speed":[np.nan]*6,"wave_height":[np.nan]*6,"temperature":[np.nan]*6,
            })
            st.session_state["race"]=race

    race=st.session_state.get("race")
    if race is not None:
        st.markdown("### ① 公式・基礎データ")
        core_cols=["lane","racer_name","racer_win_rate","local_win_rate","motor_2ren","boat_2ren","avg_st",
                   "exhibition_time","exhibition_st","weight","tilt","wind_speed","wave_height","temperature"]
        for c in core_cols:
            if c not in race: race[c]=np.nan if c!="racer_name" else ""
        race=st.data_editor(race[core_cols],use_container_width=True,hide_index=True,num_rows="fixed",key="coreedit")

        st.markdown("### ② オリジナル展示")
        st.caption("場独自の「直線・まわり足・1周」等がある場合だけ入力。小さいタイムほど高評価として扱います。")
        orig=pd.DataFrame({
            "lane":range(1,7),
            "original_straight":[np.nan]*6,
            "original_turn":[np.nan]*6,
            "original_lap":[np.nan]*6,
        })
        orig=st.data_editor(orig,use_container_width=True,hide_index=True,num_rows="fixed",key="orig")

        st.markdown("### ③ 選手コメント")
        comments=[]
        for i in range(6):
            nm=str(race.iloc[i].get("racer_name",""))
            txt=st.text_input(f"{i+1}号艇 {nm}",key=f"comment_{i}",placeholder="例：伸びはいい。回った後も悪くない")
            comments.append(txt)

        work=race.merge(orig,on="lane",how="left")
        work["comment"]=comments
        work["date"]=d.isoformat()
        work["venue"]=VENUES[jcd]
        work["race_no"]=rno

        with st.expander("コメント解析を見る"):
            for i,txt in enumerate(comments):
                if txt:
                    scores,hits=analyze_comment(txt)
                    st.write(f"**{i+1}号艇**",scores)

        odds=st.session_state.get("odds")
        st.markdown("### ④ オッズ")
        if odds is not None and len(odds):
            st.success(f"3連単オッズ {len(odds)}通りを取得")
        else:
            st.caption("自動取得できない場合はCSVをアップロード： `combo,odds`（例 `1-2-3,12.5`）")
            odds_up=st.file_uploader("オッズCSV",type="csv",key="oddsfile")
            if odds_up: odds=pd.read_csv(odds_up)

        if st.button("🤖 AI最終予想",type="primary"):
            try:
                model=train(history)
                pre=work.copy()
                pre["exhibition_time"]=np.nan
                pre["original_straight"]=np.nan
                pre["original_turn"]=np.nan
                pre["original_lap"]=np.nan
                pre["exhibition_st"]=np.nan
                pre["comment"]=""
                before=predict(model,pre,display_weight=0,comment_weight=0)
                final=predict(model,work,display_weight=display_weight,comment_weight=comment_weight)
                tri=trifecta(final)
                tickets=rank_tickets(tri,odds,main_n=int(main_n),cover_n=int(cover_n),longshot_n=int(hole_n))
                st.session_state["result"]=(before,final,tickets,work,odds)
            except Exception as e:
                st.error(str(e))

        if "result" in st.session_state:
            before,final,tickets,work,odds=st.session_state["result"]
            st.divider()
            st.subheader(f"{VENUES[jcd]} {rno}R AI最終予想")
            conf=confidence(final,work)
            st.metric("AI総合信頼度",conf)

            merged=final.merge(before[["lane","p_first"]],on="lane",suffixes=("_after","_before"))
            merged["変化"]=merged["p_first_after"]-merged["p_first_before"]
            merged=merged.sort_values("p_first_after",ascending=False)
            for _,row in merged.iterrows():
                lane=int(row["lane"]); nm=row.get("racer_name","")
                st.markdown(
                    f"""<div class="ticket"><b>{lane}号艇 {nm}</b><br>
                    1着 {row['p_first_after']*100:.1f}%　
                    <span class="small">展示前 {row['p_first_before']*100:.1f}% / {row['変化']*100:+.1f}pt</span><br>
                    <span class="small">{row['reason']}</span></div>""",unsafe_allow_html=True)

            for group,emoji in [("本線","🔥"),("抑え","🛟"),("穴","💎")]:
                st.markdown(f"### {emoji} {group}")
                g=tickets[tickets["group"]==group]
                if len(g)==0:
                    st.caption("なし")
                for _,row in g.iterrows():
                    oddtxt=f"{row['odds']:.1f}倍" if pd.notna(row["odds"]) else "オッズ未取得"
                    evtxt=f" / 期待値 {row['expected_return']:.2f}" if pd.notna(row["expected_return"]) else ""
                    st.markdown(f"""<div class="ticket"><b>{row['combo']}</b>　
                    的中確率 {row['prob']*100:.2f}%<br><span class="small">{oddtxt}{evtxt}</span></div>""",unsafe_allow_html=True)

            csv=tickets.to_csv(index=False).encode("utf-8-sig")
            st.download_button("📥 買い目CSV保存",csv,file_name=f"{d}_{VENUES[jcd]}_{rno}R_tickets.csv",mime="text/csv")

            st.warning("AI予想は確率推定であり、的中・利益を保証しません。オッズ変動、欠場・返還、展示と本番の進入差にも注意してください。")
