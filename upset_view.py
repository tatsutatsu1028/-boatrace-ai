"""
検証タブ（万舟チャンス）: 「1号艇が負けた場合の着順」専用モデル（ml_upset.py）と今のモデルを、
万舟チャンスの同じレース・同じ確定オッズで並べる（upset_compare.py が毎晩 manshu_upset_compare に書く）。
検証だけ。予想タブ・買い目・各レースの表示は変えない（合格してから相談）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import upset_compare
from pre_picks_view import _get, _pct, in_period

MIGRATION = "supabase/migrations/20261027000000_manshu_upset_compare.sql"


@st.cache_data(ttl=600, show_spinner=False)
def load_rows():
    return _get("manshu_upset_compare?select=race_date,winner_lane,is_manshu,variants"
                "&variants=not.is.null&order=race_date.asc&limit=10000")


def render(today, key="validation_upset_period"):
    st.markdown("**1号艇が負けた場合の専用モデル: 今のモデルとの比較（確定オッズ・同じレースで比較・検証用）**")
    try:
        rows = load_rows()
    except Exception as e:  # noqa: BLE001  表がまだ無いデータベース
        st.caption(f"比較を読めませんでした（{type(e).__name__}）。`{MIGRATION}` の実行が必要かもしれません。")
        return
    period = st.radio("期間", ("全期間", "直近30日", "直近7日"), horizontal=True, key=key)
    out = upset_compare.summary(in_period(rows, {"直近7日": 7, "直近30日": 30}.get(period), today))
    if not out:
        st.caption("まだありません（レースの翌日以降、事後予想の後に毎晩作ります）。")
        return
    t = pd.DataFrame(out)
    for c in t.columns:
        if c.endswith(("的中率", "回収率", "が1着")):
            t[c] = t[c].map(_pct)
    for c in ("実際の組の順位（平均）", "万舟の順位（平均）"):
        t[c] = t[c].map(lambda v: "—" if v is None or v != v else f"{v:.1f}位")
    st.dataframe(t, hide_index=True, use_container_width=True)
    st.caption("専用モデル = 1号艇が1着の組は今のモデルのまま、1号艇以外が1着の組を「今のモデルの1号艇が負ける確率 × "
               "専用モデル（1号艇が負けたレースだけで学習）の着順の確率」に置き換えた3連単の確率。"
               "朝版は展示前、展示後版は展示・直前情報も使う。1番手・上位2艇 = 1号艇以外で1着確率が高い艇"
               "（1号艇が負けたレースだけで数える）。順位 = 実際の組の3連単の確率が120通りの何位か。"
               "A = 1号艇の1着を外す・中穴5点＋万舟5点・確率順、E = 1着を上位2艇に固定・確率順10点（どちらも各100円）。"
               "画面・本番の買い目は変えていません。")
