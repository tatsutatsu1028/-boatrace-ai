"""
note 記事の表示ページ（PIN ログインなし）。app.py の一番最初で ?view=note のときだけ表示して止める。

  ?view=note&race_key=YYYYMMDD_JJ_R&key=…  そのレースの記事（タイトル・本文・価格・締切・生成時刻）
  ?view=note&key=…                         今日（日本時間）の自動下書きを締切順に一覧（各レースのページへのリンク）
  ?view=note&date=YYYY-MM-DD&key=…         その日の一覧

キー（秘密）は Supabase の vault（note_view_key）にだけあり、照合もデータベースの note_view_articles が行う。
アプリ・リポジトリにキーは無い。キーが無い・違うときは何も表示しない（エラーも出さない）。
ブラウザ操作でコピーしやすいよう、余計な UI は置かずタイトル・本文をプレーンテキストとコピーボタンで出す。
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import streamlit.components.v1 as components

JST = ZoneInfo("Asia/Tokyo")
_RACE_KEY = re.compile(r"^\d{8}_\d{2}_\d{1,2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _param(name):
    v = st.query_params.get(name)
    return str(v).strip() if v else ""


def fetch(key, race_key=None, date_iso=None):
    """note_view_articles を呼ぶ。キーが違えば []。"""
    from result_tracker import supabase_config

    url, api_key = supabase_config()
    if not url or not api_key or not key:
        return []
    r = requests.post(f"{url}/rest/v1/rpc/note_view_articles",
                      headers={"apikey": api_key, "Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      json={"p_key": key, "p_race_key": race_key or None, "p_date": date_iso or None}, timeout=10)
    r.raise_for_status()
    return r.json() or []


def _jst(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"


def _copy_block(label, text):
    """コピーボタンと、プレーンテキスト（ページ本体に出すので、選んでコピーもできる）。"""
    payload = json.dumps(text, ensure_ascii=False).replace("</", "<\\/")
    components.html(
        f"""
        <style>
          body {{ margin:0; font-family:sans-serif; }}
          button {{ min-height:40px; padding:6px 14px; cursor:pointer; font-size:14px; border-radius:8px; }}
          #s {{ font-size:13px; margin-left:8px; }}
        </style>
        <button id="b">📋 {html.escape(label)}をコピー</button><span id="s"></span>
        <script>
        const v = {payload};
        document.getElementById("b").onclick = async () => {{
          const s = document.getElementById("s");
          try {{ await navigator.clipboard.writeText(v); s.textContent = "コピーしました"; }}
          catch (e) {{ s.textContent = "コピーできませんでした（下の文字を選んでコピーしてください）"; }}
          setTimeout(() => s.textContent = "", 2500);
        }};
        </script>
        """,
        height=48,
    )
    try:
        st.code(text, language=None, wrap_lines=True)
    except TypeError:  # wrap_lines の無い古い Streamlit
        st.code(text, language=None)


def _article(row):
    title, body = str(row.get("title") or ""), str(row.get("article_text") or "")
    st.markdown(f"**{html.escape(str(row.get('venue') or ''))} {row.get('race_no')}R**")
    st.text(f"価格: {int(row.get('price') or 0)}円\n"
            f"締切: {row.get('deadline') or '—'}\n"
            f"記事の生成時刻: {_jst(row.get('created_at'))}\n"
            f"種類: {'自動下書き' if row.get('status') == 'AUTO_DRAFT' else '手動で確定した記事'}"
            f"・本文: {'Claude' if row.get('article_writer') == 'claude' else '決まった型'}")
    st.markdown("タイトル")
    _copy_block("タイトル", title)
    st.markdown("本文")
    _copy_block("本文", body)


def _list(rows, key, date_iso):
    st.markdown(f"**note 自動下書き（{date_iso}・締切順）**")
    if not rows:
        st.text("自動下書きはまだありません。")
        return
    for r in rows:
        href = f"?view=note&race_key={r['race_key']}&key={key}"
        st.markdown(f"- [{html.escape(str(r.get('venue') or ''))} {r.get('race_no')}R｜締切 {r.get('deadline') or '—'}]"
                    f"({href})（生成 {_jst(r.get('created_at'))[11:16]}）")


def render():
    """?view=note のページ。キーが合わなければ何も出さない。"""
    try:
        st.set_page_config(page_title="note記事", layout="centered", initial_sidebar_state="collapsed")
    except Exception:  # noqa: BLE001
        pass
    key, race_key, date_iso = _param("key"), _param("race_key"), _param("date")
    if not key or (race_key and not _RACE_KEY.match(race_key)) or (date_iso and not _DATE.match(date_iso)):
        return
    try:
        rows = fetch(key, race_key=race_key or None, date_iso=date_iso or None)
    except Exception:  # noqa: BLE001  キーの有無が分からないように何も出さない
        return
    if race_key:
        if rows:
            _article(rows[0])
        return
    # 一覧: キーが違うときも0行なので、キーが正しいと分かったとき（照合用に1回）だけ出す
    if not rows and not _key_ok(key):
        return
    _list(rows, key, date_iso or datetime.now(JST).strftime("%Y-%m-%d"))


def _key_ok(key):
    """一覧が0行のとき、キー自体が正しいか（存在しないレースを1件引いて確かめることはできないので、関数で照合）。"""
    from result_tracker import supabase_config

    url, api_key = supabase_config()
    try:
        r = requests.post(f"{url}/rest/v1/rpc/note_view_key_ok",
                          headers={"apikey": api_key, "Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          json={"p_key": key}, timeout=10)
        r.raise_for_status()
        return r.json() is True
    except Exception:  # noqa: BLE001
        return False
