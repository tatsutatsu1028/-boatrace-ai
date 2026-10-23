"""
スレッズの朝の自動投稿（threads_morning.py）の設定と記録の表示（管理者だけ）。

  - render_settings(): 設定タブ。自動投稿・結果の自動投稿の ON/OFF と、直近の投稿の記録（失敗はエラーで出す）
  - render_status(d) : 予想タブ。その日の朝の投稿の結果（失敗は目立つように）
"""

from __future__ import annotations

from datetime import timedelta

import requests
import streamlit as st

from result_tracker import supabase_config

STATUS_LABELS = {"posted": "✅ 投稿済み", "partial": "⚠️ 途中まで投稿", "failed": "❌ 失敗", "skipped": "⏸ 投稿なし"}
RETRY_NOTE = ("やり直すときは GitHub Actions の「Threads Morning Post」を retry = true で手動実行してください"
              "（途中で止まった日は、締切まで30分以上ある残りの投稿だけを出し直します）。")


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


def load_enabled(column="threads_auto_post_enabled"):
    try:
        rows = _get(f"app_settings?id=eq.1&select={column}")
        return bool(rows and rows[0].get(column))
    except Exception:  # noqa: BLE001  列がまだ無いデータベースなど
        return None


def save_enabled(enabled, column="threads_auto_post_enabled"):
    url, key = supabase_config()
    r = requests.patch(f"{url}/rest/v1/app_settings?id=eq.1", headers=_headers(key, "return=minimal"),
                       json={column: bool(enabled)}, timeout=10)
    r.raise_for_status()


@st.cache_data(ttl=60, show_spinner=False)
def load_log(since_iso):
    return _get(f"threads_auto_posts?race_date=gte.{since_iso}&select=*&order=race_date.desc")


@st.cache_data(ttl=60, show_spinner=False)
def load_result_log(since_iso):
    """結果の投稿の記録 {日付: [行]}。表がまだ無ければ空。"""
    try:
        rows = _get(f"threads_result_posts?race_date=gte.{since_iso}&select=*&order=race_date.desc")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        out.setdefault(r["race_date"], []).append(r)
    return out


def _race(p):
    return f"{p.get('venue')}{p.get('race_no')}R（締切 {p.get('deadline') or '--:--'}）"


RESULT_STATUS_LABELS = {"waiting": "結果待ち", "posting": "投稿中", "posted": "投稿済み", "failed": "失敗"}
OUTCOME_LABELS = {"hit": "的中", "miss": "不的中", "cancelled": "中止", "refunded": "返還"}


def _result_item_label(r):
    if r.get("item") == "summary":
        name = "まとめ"
        if r.get("settled") is not None:
            name += f"（{r['settled']}レース中{r.get('hits') or 0}レース的中）"
    else:
        name = f"{r.get('venue') or ''}{r.get('race_no') or ''}R"
        if r.get("outcome"):
            name += f"（{OUTCOME_LABELS.get(r['outcome'], r['outcome'])}）"
    when = str(r.get("posted_at") or r.get("attempted_at") or "")[11:16]
    status = RESULT_STATUS_LABELS.get(r.get("status"), r.get("status"))
    return f"{name} {status}" + (f" {when}" if when and r.get("status") in ("posted", "failed") else "")


def _render_results(results, compact=False):
    """その日の結果の投稿の記録（朝の投稿の順、まとめは最後）。"""
    if not results:
        return
    results = sorted(results, key=lambda r: (r.get("item") == "summary", r.get("post_index") or 0))
    line = "🏁 結果の投稿: " + "、".join(_result_item_label(r) for r in results)
    stopped = [r for r in results if r.get("status") == "failed" or r.get("reason")]
    if stopped:
        st.error(line + "".join(f"\n\n{_result_item_label(r)}: {r.get('reason') or ''}"
                                 + (f"（{r['error']}）" if r.get("error") else "") for r in stopped))
    else:
        st.caption(line)
    if not compact and any(r.get("text") for r in results):
        with st.expander("結果の投稿の本文", expanded=False):
            for r in results:
                if r.get("text"):
                    st.caption(_result_item_label(r))
                    st.code(r["text"], language=None)


def _render_row(row, compact=False, results=None):
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
    if row.get("image_error") and posted:
        st.warning(f"🖼 画像なしで投稿しました（{row['image_error']}）")
    if not compact and posted:
        with st.expander("投稿した本文", expanded=False):
            if row.get("image_url"):
                st.image(row["image_url"], caption="1投稿目の画像", width=320)
            for p in row.get("posts") or []:
                st.caption(f"{_race(p)}　買い目: モデルのみ予想" + ("　（失敗）" if p.get("error") else ""))
                st.code(p.get("text") or "", language=None)
    _render_results(results, compact=compact)


def render_settings(today):
    st.markdown("#### 🌅 朝の無料3レース（自動投稿）")
    st.caption("毎朝9:00に、「🌅 展示前厳選10」から締切まで30分以上ある的中確率の高い3レースを、"
               "スレッド形式（1投稿目に1レース目、返信で2・3レース目）でスレッズに投稿します。買い目はモデルのみ予想です。"
               "厳選が3レースそろわない日・データに異常がある日は投稿しません。")
    _toggle("threads_auto_post_enabled", "朝の自動投稿")
    st.caption("結果の自動投稿: 朝に出した3レースの結果が確定したら（5分おきの結果確定のあと）、そのレースの投稿への返信で"
               "結果（3連単の着順・配当、的中なら当たった買い目、外れなら不的中）を投稿します。3レースとも結果が出たら、"
               "1投稿目への返信で結果まとめを投稿します。朝の自動投稿もONで、朝の3投稿が全部出た日だけ投稿します。"
               "失敗は次の回にやり直し、3回失敗したら止めます。")
    _toggle("threads_result_post_enabled", "結果の自動投稿")
    since = (today - timedelta(days=13)).isoformat()
    try:
        rows = load_log(since)
    except Exception as e:  # noqa: BLE001
        st.caption(f"投稿の記録を読めませんでした（{type(e).__name__}）")
        return
    if not rows:
        st.caption("投稿の記録はまだありません。")
        return
    st.caption("直近2週間の記録")
    results = load_result_log(since)
    for row in rows:
        _render_row(row, results=results.get(row["race_date"]))


def _toggle(column, label):
    enabled = load_enabled(column)
    if enabled is None:
        st.caption(f"{label}の設定を読めませんでした。")
        return
    new = st.toggle(f"{label}をONにする", value=enabled, key=column)
    if new != enabled:
        try:
            save_enabled(new, column)
            st.success(f"{label}をONにしました。" if new else f"{label}をOFFにしました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存できませんでした: {e}")


def render_status(d):
    """予想タブ: その日の朝の投稿の結果（記録が無ければ何も出さない）。"""
    try:
        rows = load_log(d.isoformat())
    except Exception:  # noqa: BLE001
        return
    row = next((r for r in rows if r.get("race_date") == d.isoformat()), None)
    if row:
        st.markdown("##### 🧵 朝の無料3レース（スレッズ）")
        _render_row(row, compact=True, results=load_result_log(d.isoformat()).get(d.isoformat()))
