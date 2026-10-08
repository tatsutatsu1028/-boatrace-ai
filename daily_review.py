"""
管理者向け: その日の予想と結果の照合一覧、と、結果の手動保存（自動で入らなかったときの予備）。

照合一覧（render_daily）
  - その日に予想を保存したレース（prediction_snapshots）と結果（prediction_results）を並べ、
    実際の3連単・モデルの買い目の的中・モデル＋Claudeの買い目の的中・それぞれの払戻を出す
  - Supabase からは、その日の行だけ・一覧に要る列だけを取る（予想の中身 payload_json は取らない）
  - 結果が無いレースは「結果待ち」。Claude の読みが無いレース（自動固定など）の モデル＋Claude は「－」

手動保存（render_manual_save）
  - 予想タブにあった「結果取得＋検証保存」と同じ処理を、日付とレースを選んで行う
  - 保存済みの予想（固定予想）だけで判定する（画面の予想は使わない）
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from result_tracker import _headers, supabase_config

def _today_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


SNAPSHOT_COLS = "race_key,venue,race_no,collector_name,snapshot_kind,saved_at"
RESULT_COLS = (
    "race_key,trifecta_actual,hit_any_ticket,payout,total_stake,"
    "claude_hit_any_ticket,claude_payout,claude_total_stake,"
    "mix_hit_any_ticket,mix_payout,mix_total_stake"
)
# 照合する3つの予想（表示名, 的中の列, 払戻の列, 購入額の列）。モデルのみは既存の列
PREDICTIONS = [
    ("モデル", "hit_any_ticket", "payout", "total_stake"),
    ("Claude", "claude_hit_any_ticket", "claude_payout", "claude_total_stake"),
    ("平均", "mix_hit_any_ticket", "mix_payout", "mix_total_stake"),
]
PREDICTION_TITLES = {"モデル": "モデルのみ予想", "Claude": "Claudeのみ予想", "平均": "両方の予想平均"}


@st.cache_data(ttl=60, show_spinner=False)
def load_day(race_date):
    """その日の予想（一覧に要る列だけ）と結果（照合に要る列だけ）。1分だけ覚えておく。"""
    url, _ = supabase_config()
    if not url:
        raise RuntimeError("Supabase の設定がありません")
    day = str(race_date)
    snaps = requests.get(
        f"{url}/rest/v1/prediction_snapshots",
        params={"select": SNAPSHOT_COLS, "race_date": f"eq.{day}", "order": "race_key"},
        headers=_headers(), timeout=15,
    )
    snaps.raise_for_status()
    res = requests.get(
        f"{url}/rest/v1/prediction_results",
        params={"select": RESULT_COLS, "race_date": f"eq.{day}"},
        headers=_headers(), timeout=15,
    )
    res.raise_for_status()
    return pd.DataFrame(snaps.json() or []), pd.DataFrame(res.json() or [])


def _yen(v):
    try:
        return f"{int(v):,}円"
    except (TypeError, ValueError):
        return "－"


def _mark(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "－"
    return "○" if bool(v) else "×"


def _has(v):
    return v is not None and not (isinstance(v, float) and pd.isna(v))


def build_table(snaps, results, venue_names=None):
    """照合一覧の表と、その日の合計（3つの予想ごと）。"""
    if snaps is None or snaps.empty:
        return pd.DataFrame(), {}
    res = results.set_index("race_key") if results is not None and len(results) else pd.DataFrame()
    rows = []
    tot = {name: {"races": 0, "hits": 0, "payout": 0, "stake": 0} for name, *_ in PREDICTIONS}
    tot["pending"] = 0
    for s in snaps.itertuples():
        jcd = str(s.race_key).split("_")[1] if "_" in str(s.race_key) else ""
        venue = (venue_names or {}).get(jcd) or s.venue
        kind = "自動" if str(s.collector_name or "") == "auto_random" else "手動"
        row = {"レース": f"{venue} {int(s.race_no)}R", "予想": kind}
        r = res.loc[s.race_key] if len(res) and s.race_key in res.index else None
        if r is None:
            tot["pending"] += 1
            row["実際の3連単"] = "結果待ち"
            for name, *_ in PREDICTIONS:
                row[name] = "結果待ち"
                row[f"{name}払戻"] = ""
            rows.append(row)
            continue
        row["実際の3連単"] = str(r.get("trifecta_actual") or "－")
        for name, hit_col, pay_col, stake_col in PREDICTIONS:
            if not _has(r.get(hit_col)):  # Claude の予想が無いレース（モデルのみ・自動固定など）
                row[name], row[f"{name}払戻"] = "－", "－"
                continue
            t = tot[name]
            t["races"] += 1
            t["hits"] += int(bool(r.get(hit_col)))
            t["payout"] += int(r.get(pay_col) or 0)
            t["stake"] += int(r.get(stake_col) or 0)
            row[name] = _mark(r.get(hit_col))
            row[f"{name}払戻"] = _yen(r.get(pay_col))
        rows.append(row)
    return pd.DataFrame(rows), tot


def render_daily(venue_names=None):
    st.markdown("### 📋 今日の照合")
    day = st.date_input("日付", value=_today_jst(), key="daily_review_date")
    try:
        snaps, results = load_day(day.isoformat())
    except Exception as e:  # noqa: BLE001
        st.caption(f"照合一覧を読み込めませんでした（{type(e).__name__}）")
        return
    table, tot = build_table(snaps, results, venue_names)
    if table.empty:
        st.caption("この日に予想を保存したレースはありません。")
        return
    st.dataframe(table, use_container_width=True, hide_index=True)

    def roi(p, s):
        return f"{p / s * 100:.0f}%" if s else "－"

    cols = st.columns(3)
    for col, (name, *_) in zip(cols, PREDICTIONS):
        t = tot[name]
        with col:
            st.metric(f"{PREDICTION_TITLES[name]}（{t['races']}R）", f"{t['hits']}本 的中",
                      f"払戻 {t['payout']:,}円 ／ 回収率 {roi(t['payout'], t['stake'])}", delta_color="off")
    st.caption(
        "的中は金額を付けた買い目に実際の3連単が入っていたか。払戻は1レースの予算どおりに買った場合。"
        + (f" 結果待ち {tot['pending']}R。" if tot["pending"] else "")
        + " Claude・平均は、管理者が「Claudeのみ予想」か「両方の予想平均」でClaudeの予想を取ったレースだけ"
        "（それ以外は「－」）。1分ごとに読み直します。"
    )
    if st.button("🔄 照合一覧を読み直す", key="daily_review_reload"):
        load_day.clear()
        st.rerun()


# ---------------------------------------------------------------
# 手動の結果保存（予備）
# ---------------------------------------------------------------
def render_manual_save(venue_names, collector_name):
    from official_fetcher import fetch_race_result
    from result_tracker import (deactivate_odds_watchlist, load_prediction_snapshot, restore_snapshot_frames,
                                save_race_result, snapshot_payout_from_official)

    with st.expander("🔧 結果の手動保存（自動で入らなかったときの予備）", expanded=False):
        st.caption("結果は自動で保存されます。自動で入らなかったレースだけ、ここから保存してください。"
                   "保存済みの予想（固定予想）で判定します。")
        day = st.date_input("日付", value=_today_jst(), key="manual_save_date")
        try:
            snaps, results = load_day(day.isoformat())
        except Exception as e:  # noqa: BLE001
            st.caption(f"予想の一覧を読み込めませんでした（{type(e).__name__}）")
            return
        if snaps.empty:
            st.caption("この日に予想を保存したレースはありません。")
            return
        done = set(results["race_key"]) if len(results) else set()
        only_pending = st.checkbox("結果が未保存のレースだけ", value=True, key="manual_save_pending")
        keys = [k for k in snaps["race_key"] if not (only_pending and k in done)]
        if not keys:
            st.caption("結果が未保存のレースはありません。")
            return
        labels = {r.race_key: f"{venue_names.get(str(r.race_key).split('_')[1], r.venue)} {int(r.race_no)}R"
                  + ("（保存済み）" if r.race_key in done else "") for r in snaps.itertuples()}
        rk = st.selectbox("レース", keys, format_func=lambda k: labels.get(k, k), key="manual_save_race")
        hd, jcd, rno = str(rk).split("_")
        rno = int(rno)

        def _save(first, second, third, payout, snapshot):
            final, tickets, research = restore_snapshot_frames(snapshot)
            return save_race_result(
                race_key=rk, race_date=pd.Timestamp(hd).date().isoformat(),
                venue=venue_names.get(jcd, ""), race_no=rno, final=final, tickets=tickets,
                first_actual=first, second_actual=second, third_actual=third, payout=int(payout),
                research_variants=research or {}, prefer_snapshot=True, snapshot=snapshot,
                require_snapshot=True, collector_name=collector_name,
            )

        if st.button("🏁 公式結果を取得して保存", key="manual_save_official"):
            snapshot = load_prediction_snapshot(rk)
            if snapshot is None:
                st.error("保存済みの予想を読み込めませんでした。")
            else:
                try:
                    off = fetch_race_result(hd, jcd, rno)
                    received, _ = snapshot_payout_from_official(
                        rk, off["trifecta"], off["trifecta_payout_per_100"], snapshot=snapshot)
                    rec = _save(int(off["first"]), int(off["second"]), int(off["third"]), received, snapshot)
                    deactivate_odds_watchlist(hd, jcd, rno)
                    load_day.clear()
                    st.success(f"保存しました：実結果 {rec['trifecta_actual']} / "
                               f"購入買い目 {'的中' if rec['hit_any_ticket'] else '不的中'} / "
                               f"収支 {rec['profit']:+,}円")
                except Exception as e:  # noqa: BLE001
                    st.warning("公式結果の取得または保存を完了できませんでした。結果確定後にもう一度押してください。")
                    st.caption(str(e))

        st.caption("公式結果が取れないときだけ、着順と実際の受取額を入れて保存できます。")
        c1, c2, c3 = st.columns(3)
        a1 = c1.selectbox("実1着", range(1, 7), key="manual_save_a1")
        a2 = c2.selectbox("実2着", range(1, 7), index=1, key="manual_save_a2")
        a3 = c3.selectbox("実3着", range(1, 7), index=2, key="manual_save_a3")
        pay = st.number_input("実払戻受取額（円）", min_value=0, max_value=10_000_000, value=0, step=100,
                              key="manual_save_payout")
        if len({a1, a2, a3}) < 3:
            st.warning("1着・2着・3着は別々の艇を選んでください。")
        elif st.button("💾 入力した結果で保存", key="manual_save_input"):
            snapshot = load_prediction_snapshot(rk)
            if snapshot is None:
                st.error("保存済みの予想を読み込めませんでした。")
            else:
                rec = _save(a1, a2, a3, pay, snapshot)
                load_day.clear()
                st.success(f"保存しました：実結果 {rec['trifecta_actual']} / "
                           f"購入買い目 {'的中' if rec['hit_any_ticket'] else '不的中'}")
