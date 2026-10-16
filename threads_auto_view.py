"""
スレッズの朝の自動投稿（threads_morning.py）の設定と記録の表示（管理者だけ）。

  - render_settings(): 設定タブ。自動投稿の ON/OFF と、直近の投稿の記録（失敗はエラーで出す）
  - render_status(d) : 予想タブ。その日の朝の投稿の結果（失敗・note との重複は目立つように）
"""

from __future__ import annotations

from datetime import timedelta

import requests
import streamlit as st

from result_tracker import supabase_config

STATUS_LABELS = {"posted": "✅ 投稿済み", "partial": "⚠️ 途中まで投稿", "failed": "❌ 失敗", "skipped": "⏸ 投稿なし"}
RETRY_NOTE = ("やり直すときは GitHub Actions の「Threads Morning Post」を retry = true で手動実行してください"
              "（1投稿もできなかった日だけやり直せます）。")


def _headers(key, prefer=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _get(path):
    url, key = supabase_config()
    if not url or not key:
        raise RuntimeError("Supabase の設定がありません")
    r = requests.get(f"{url}/rest/v1/{path}", headers=_headers(key), timeout=10)
    r.raise_for_status()
    return r.json() or []


def load_enabled():
    try:
        rows = _get("app_settings?id=eq.1&select=threads_auto_post_enabled")
        return bool(rows and rows[0].get("threads_auto_post_enabled"))
    except Exception:  # noqa: BLE001
        return None


def save_enabled(enabled):
    url, key = supabase_config()
    r = requests.patch(f"{url}/rest/v1/app_settings?id=eq.1", headers=_headers(key, "return=minimal"),
                       json={"threads_auto_post_enabled": bool(enabled)}, timeout=10)
    r.raise_for_status()


@st.cache_data(ttl=60, show_spinner=False)
def load_log(since_iso):
    return _get(f"threads_auto_posts?race_date=gte.{since_iso}&select=*&order=race_date.desc")


def note_overlap(row):
    """投稿したレースのうち、note の記事にもなっているレース（投稿の後に記事にしたものも含める）。"""
    posted = [p for p in (row.get("posts") or []) if p.get("post_id")]
    keys = {str(p.get("race_key")) for p in posted}
    found = set(row.get("note_overlap") or []) & keys
    if keys:
        try:
            found |= {str(r["race_key"]) for r in _get(
                f"note_publications?race_key=in.({','.join(sorted(keys))})&select=race_key")}
        except Exception:  # noqa: BLE001
            pass
    return [p for p in posted if str(p.get("race_key")) in found]


def _race(p):
    return f"{p.get('venue')}{p.get('race_no')}R（締切 {p.get('deadline') or '--:--'}）"


def _render_row(row, compact=False):
    status = row.get("status")
    posted = [p for p in (row.get("posts") or []) if p.get("post_id")]
    head = f"{row['race_date']}　{STATUS_LABELS.get(status, status)}"
    races = "、".join(_race(p) for p in posted)
    if status == "posted":
        st.success(f"{head}　{races}")
    elif status in ("failed", "partial"):
        failed = next((p for p in (row.get("posts") or []) if p.get("error")), None)
        st.error(f"{head}　{row.get('reason') or ''}"
                 + (f"　投稿できたレース: {races}" if races else "")
                 + (f"\n\n失敗の内容: {failed['error']}" if failed else ""))
        st.caption(RETRY_NOTE)
    else:
        st.info(f"{head}　理由: {row.get('reason') or '—'}")
    overlap = note_overlap(row)
    if overlap:
        st.warning("⚠️ 朝の無料投稿と同じレースを note の有料記事にしています: " + "、".join(_race(p) for p in overlap))
    if not compact and posted:
        with st.expander("投稿した本文", expanded=False):
            for p in row.get("posts") or []:
                st.caption(f"{_race(p)}　買い目: モデルのみ予想" + ("　（失敗）" if p.get("error") else ""))
                st.code(p.get("text") or "", language=None)


def render_settings(today):
    st.markdown("#### 🌅 朝の無料3レース（自動投稿）")
    st.caption("毎朝9:00に、「🌅 展示前厳選10」から締切まで30分以上ある的中確率の高い3レースを、"
               "スレッド形式（1投稿目に1レース目、返信で2・3レース目）でスレッズに投稿します。買い目はモデルのみ予想です。"
               "厳選が3レースそろわない日・データに異常がある日は投稿しません。")
    enabled = load_enabled()
    if enabled is None:
        st.caption("自動投稿の設定を読めませんでした。")
    else:
        new = st.toggle("朝の自動投稿をONにする", value=enabled, key="threads_auto_post_enabled")
        if new != enabled:
            try:
                save_enabled(new)
                st.success("朝の自動投稿をONにしました。" if new else "朝の自動投稿をOFFにしました。")
            except Exception as e:  # noqa: BLE001
                st.error(f"保存できませんでした: {e}")
    try:
        rows = load_log((today - timedelta(days=13)).isoformat())
    except Exception as e:  # noqa: BLE001
        st.caption(f"投稿の記録を読めませんでした（{type(e).__name__}）")
        return
    if not rows:
        st.caption("投稿の記録はまだありません。")
        return
    st.caption("直近2週間の記録")
    for row in rows:
        _render_row(row)


def render_status(d):
    """予想タブ: その日の朝の投稿の結果（記録が無ければ何も出さない）。"""
    try:
        rows = load_log(d.isoformat())
    except Exception:  # noqa: BLE001
        return
    row = next((r for r in rows if r.get("race_date") == d.isoformat()), None)
    if row:
        st.markdown("##### 🧵 朝の無料3レース（スレッズ）")
        _render_row(row, compact=True)
