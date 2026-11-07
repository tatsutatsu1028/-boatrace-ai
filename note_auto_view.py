"""
設定タブの「📝 note 自動下書き」（管理者だけ）。note_auto.py・note_page.py の設定と今日の記録。

  - 自動下書きの ON/OFF（app_settings.note_auto_enabled）
  - 記事に載せる買い目の既定（app_settings.note_auto_ticket_source: モデルのみ予想 / 両方の予想平均）
  - 記事表示ページの URL の頭（app_settings.note_view_base_url。このアプリの URL）
  - 記事表示ページの一覧の URL（キー付き。service_key でだけ読める note_view_link）
  - 今日の記録（auto_post_log.note_status・作るのにかかった秒数）
"""

from __future__ import annotations

import requests
import streamlit as st

from result_tracker import supabase_config, supabase_service_config

STATUS_LABELS = {"ok": "✅ 作成", "template": "⚠️ 記事生成失敗（買い目だけの型で保存）", "manual": "📝 手動の記事あり",
                 "failed": "❌ 記事生成失敗"}
SOURCE_LABELS = {"model": "モデルのみ予想", "avg": "両方の予想平均"}


def _h(key, prefer=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _load():
    url, key = supabase_config()
    r = requests.get(f"{url}/rest/v1/app_settings?id=eq.1&select=note_auto_enabled,note_auto_ticket_source,"
                     "note_view_base_url", headers=_h(key), timeout=10)
    r.raise_for_status()
    rows = r.json() or []
    return rows[0] if rows else None


def _save(values):
    url, key = supabase_config()
    r = requests.patch(f"{url}/rest/v1/app_settings?id=eq.1", headers=_h(key, "return=minimal"), json=values,
                       timeout=10)
    r.raise_for_status()


def _link():
    url, key = supabase_service_config()
    if not url or not key:
        return None
    r = requests.post(f"{url}/rest/v1/rpc/note_view_link", headers=_h(key), json={}, timeout=10)
    r.raise_for_status()
    return r.json()


def _today_log(date_iso):
    url, key = supabase_config()
    r = requests.get(f"{url}/rest/v1/auto_post_log?race_date=eq.{date_iso}&note_status=not.is.null"
                     "&select=venue,race_no,deadline,note_status,note_reason,note_seconds&order=deadline.asc",
                     headers=_h(key), timeout=10)
    r.raise_for_status()
    return r.json() or []


def render_settings(today):
    st.markdown("#### 📝 note 自動下書き（締切前の通知と同時）")
    st.caption("締切前の Discord 通知（展示前厳選10・万舟チャンス）のレースで、展示後の予想を保存したらすぐに note の有料記事の"
               "下書き（本文は Claude Haiku・無料部分と有料部分の境目に「【有料ライン】」の行）を作ります。通知は記事ができるのを"
               "待って（最大3分）、記事表示ページの URL を載せます。作れなかったレースは通知に「記事生成失敗」と出ます。"
               "下書きは「確定済みの記事」（本日N本目・夜のまとめ）には数えません。予想タブで「note記事用に確定」を押すと手動の記事に置き換わります。")
    try:
        cfg = _load()
    except Exception as e:  # noqa: BLE001  列がまだ無いデータベースなど
        st.caption(f"設定を読めませんでした（{type(e).__name__}）。")
        return
    if not cfg:
        st.caption("設定（app_settings）がありません。")
        return
    on = st.toggle("note 自動下書きをONにする", value=bool(cfg.get("note_auto_enabled")), key="note_auto_enabled")
    src_now = cfg.get("note_auto_ticket_source") if cfg.get("note_auto_ticket_source") in SOURCE_LABELS else "model"
    src = st.radio("記事に載せる買い目（既定）", list(SOURCE_LABELS), index=list(SOURCE_LABELS).index(src_now),
                   format_func=SOURCE_LABELS.get, horizontal=True, key="note_auto_ticket_source")
    if src == "avg":
        st.caption("両方の予想平均は、レースごとに Claude の読み（予想タブの 🧠 と同じモデル）を取るため、記事ができるまで"
                   "数十秒長くかかり、費用も増えます。読みを取れなかったレースはモデルのみ予想にします。")
    base = st.text_input("記事表示ページの URL（このアプリの URL。例 https://xxxx.streamlit.app）",
                         value=cfg.get("note_view_base_url") or "", key="note_view_base_url")
    changed = {k: v for k, v in (("note_auto_enabled", on), ("note_auto_ticket_source", src),
                                 ("note_view_base_url", base.strip() or None))
               if v != (cfg.get(k) if k != "note_auto_ticket_source" else src_now)}
    if changed:
        try:
            _save(changed)
            st.success("保存しました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存に失敗しました（{type(e).__name__}）。")
    try:
        link = _link()
    except Exception:  # noqa: BLE001
        link = None
    if link:
        st.caption("記事表示ページ（今日の自動下書きの一覧）。キー付きなので人に見せないでください:")
        st.code(link, language=None)
    else:
        st.caption("記事表示ページの URL は、上の URL を入れると出ます（Secrets の supabase.service_key が必要）。")
    try:
        rows = _today_log(today.isoformat())
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        st.caption("今日の記録（作るのにかかった秒数）")
        for r in rows:
            sec = r.get("note_seconds")
            st.caption(f"{r.get('deadline') or '--:--'} {r.get('venue')}{r.get('race_no')}R　"
                       f"{STATUS_LABELS.get(r.get('note_status'), r.get('note_status'))}"
                       + (f"（{sec:.0f}秒）" if isinstance(sec, (int, float)) else "")
                       + (f"　{r['note_reason']}" if r.get("note_reason") else ""))
