"""
「🧵 スレッズに投稿」の欄（管理者だけ）。予想タブと「💥 万舟チャンス」「🌅 展示前厳選10」の一覧で共通。

  - 文章は race_posts.threads_text（予想タブと一覧で同じ文章）。万舟チャンスのレースは
    「通常の予想」か「中穴〜万舟狙い」を選ぶ
  - 投稿したら threads_race_posts に記録する（どの画面から・いつ・投稿ID・本文）。同じレースで投稿済みなら
    「投稿済み（時刻）」と出し、もう一度投稿するときは確認を挟む
  - 毎朝9:00の自動投稿（厳選上位3レース）と同じレースなら、その旨を出す（これも確認を挟む）

threads_race_posts はスレッズの設定と同じく Secret key（service_key）で読み書きする（RLS で anon は読めない）。
"""

from __future__ import annotations

import requests
import streamlit as st

import race_posts
from result_tracker import supabase_service_config

TABLE = "threads_race_posts"
MIGRATION = "supabase/migrations/20261026000000_threads_race_posts.sql"


def _headers(key, prefer=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


@st.cache_data(ttl=60, show_spinner=False)
def load_day_posts(date_iso):
    """その日にこのアプリから投稿したスレッズの記録。表がまだ無い・読めなければ空。"""
    url, key = supabase_service_config()
    if not url or not key:
        return []
    try:
        r = requests.get(f"{url}/rest/v1/{TABLE}?race_date=eq.{date_iso}&select=race_key,posted_at,post_id,screen,"
                         "style&order=posted_at.asc", headers=_headers(key), timeout=10)
        r.raise_for_status()
        return r.json() or []
    except Exception:  # noqa: BLE001
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_day_auto(date_iso):
    """その日の朝9:00の自動投稿の記録（threads_auto_posts の1行）。無ければ None。"""
    import threads_auto_view

    try:
        return next((r for r in threads_auto_view.load_log(date_iso) if str(r.get("race_date")) == date_iso), None)
    except Exception:  # noqa: BLE001
        return None


def race_status(race_key, notes=()):
    iso = f"{race_key[:4]}-{race_key[4:6]}-{race_key[6:8]}"
    return race_posts.posted_status(race_key, notes, load_day_posts(iso), load_day_auto(iso))


def _record(row):
    url, key = supabase_service_config()
    r = requests.post(f"{url}/rest/v1/{TABLE}", headers=_headers(key, "return=minimal"), json=row, timeout=10)
    r.raise_for_status()
    load_day_posts.clear()


def load_config():
    """スレッズの連携設定（投稿に使う）。未設定・読めなければ None。"""
    try:
        import threads_poster as tp
    except Exception:  # noqa: BLE001
        return None
    url, key = supabase_service_config()
    if not url or not key:
        return None
    try:
        return tp.load_config(url, key)
    except Exception:  # noqa: BLE001
        return None


def render(ctx, d, venue, rno, deadline, final, tickets, pub, cfg, is_manshu=False, key_prefix="", screen="predict",
           prediction="", default_style=None):
    """スレッズ投稿の欄（expander の中身）。cfg はスレッズの連携設定。"""
    import threads_poster as tp

    style = "normal"
    if is_manshu:
        opts = ["normal", "aim"]
        first = default_style or race_posts.style_for((pub or {}).get("ticket_source"))
        style = st.radio("スレッズの文章", opts, index=opts.index(first) if first in opts else 0,
                         format_func=lambda m: race_posts.STYLE_LABELS[m], horizontal=True,
                         key=f"{key_prefix}threads_style_{ctx}")
    default_text = race_posts.threads_text(d, venue, rno, final, tickets, deadline, pub, style)

    status = race_status(ctx)
    session_post = st.session_state.get(f"threads_posted_{ctx}")
    for line in race_posts.status_lines({**status, "note": None}):
        st.warning(line)
    if session_post and not status["threads"]:
        st.warning(f"🧵 このレースはこの画面で投稿済みです（投稿ID: {session_post}）。")

    text = st.text_area("投稿内容（送信前に編集できます）", value=default_text, height=240,
                        key=f"{key_prefix}threads_text_v2_{ctx}_{style}")
    n = len(text)
    if n > tp.TEXT_LIMIT:
        st.error(f"{n} / {tp.TEXT_LIMIT}文字（超過しています）")
    else:
        st.caption(f"{n} / {tp.TEXT_LIMIT}文字")

    again = race_posts.needs_confirm(status) or bool(session_post)
    ok = True
    if again:
        ok = st.checkbox("投稿済みのレースです。もう一度投稿する（二重投稿になります）",
                         key=f"{key_prefix}threads_again_{ctx}")
    if st.button("🧵 この内容でスレッズに投稿", key=f"{key_prefix}post_threads_{ctx}",
                 disabled=n > tp.TEXT_LIMIT or not ok, use_container_width=True):
        try:
            post_id = tp.post_text(cfg["user_id"], cfg["access_token"], text)
        except Exception as e:  # noqa: BLE001
            st.error("スレッズへの投稿に失敗しました。")
            st.code(str(e))
            st.caption("トークンが失効している可能性があります。設定タブから再登録してください。")
            return
        st.session_state[f"threads_posted_{ctx}"] = post_id
        try:
            _record(race_posts.threads_record(ctx, post_id, text, style, (pub or {}).get("ticket_source"),
                                              prediction, screen))
        except Exception as e:  # noqa: BLE001
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            st.warning(f"投稿しましたが、投稿済みの記録を残せませんでした（{type(e).__name__}"
                       f"{f' {status_code}' if status_code else ''}）。`{MIGRATION}` の実行が必要かもしれません。")
            st.success(f"投稿しました。（投稿ID: {post_id}）")
            return
        st.rerun()
