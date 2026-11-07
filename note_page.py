"""
note 記事の表示ページ（PIN ログインなし）。app.py の一番最初で ?view=note のときだけ表示して止める。

  ?view=note&race_key=YYYYMMDD_JJ_R&key=…  そのレースの記事（タイトル・本文・価格・締切・生成時刻）
  ?view=note&key=…                         今日（日本時間）の自動下書きを締切順に一覧（各レースのページへのリンク）
  ?view=note&date=YYYY-MM-DD&key=…         その日の一覧
  ?view=note&report=YYYY-MM-DD&key=…       その日の結果報告記事（1日の締め。note_report.py）

キー（秘密）は Supabase の vault（note_view_key）にだけあり、照合もデータベースの note_view_articles が行う。
アプリ・リポジトリにキーは無い。キーが無い・違うときは何も表示しない（エラーも出さない）。
ブラウザ操作でコピーしやすいよう、タイトル・無料部分・有料部分・ハッシュタグをプレーンテキストとコピーボタンで出す
（無料部分・有料部分は【有料ライン】の行で分ける。段落が分かれたまま貼れるよう HTML でもコピーする）。
見出し画像（保存ボタン）と、公開した note 記事の URL の登録（note_view_register。確定済みの記事・夜のまとめに入る）もある。
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


def _rpc(name, payload):
    from result_tracker import supabase_config

    url, api_key = supabase_config()
    if not url or not api_key:
        raise RuntimeError("Supabase の設定がありません")
    r = requests.post(f"{url}/rest/v1/rpc/{name}",
                      headers={"apikey": api_key, "Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"}, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch(key, race_key=None, date_iso=None):
    """note_view_articles を呼ぶ。キーが違えば []。"""
    if not key:
        return []
    return _rpc("note_view_articles_v2", {"p_key": key, "p_race_key": race_key or None, "p_date": date_iso or None}) or []


def _jst(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"


def _paste_html(text):
    """空行で区切った段落 → 段落ごとの <p>（note に貼っても段落が分かれたままになる）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text or "").strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(ln) for ln in p.splitlines()) + "</p>" for p in paras)


def _copy_block(label, text, rich=False):
    """コピーボタンと、プレーンテキスト（ページ本体に出すので、選んでコピーもできる）。
    rich=True は段落ごとの <p>（HTML）も一緒にコピーする（note の本文に貼る用）。"""
    payload = json.dumps([text, _paste_html(text) if rich else ""], ensure_ascii=False).replace("</", "<\\/")
    components.html(
        f"""
        <style>
          body {{ margin:0; font-family:sans-serif; }}
          button {{ min-height:40px; padding:6px 14px; cursor:pointer; font-size:14px; border-radius:8px; }}
          #s {{ font-size:13px; margin-left:8px; }}
        </style>
        <button id="b">📋 {html.escape(label)}をコピー</button><span id="s"></span>
        <script>
        const [plain, rich] = {payload};
        document.getElementById("b").onclick = async () => {{
          const s = document.getElementById("s");
          try {{
            if (rich && window.ClipboardItem && navigator.clipboard.write) {{
              await navigator.clipboard.write([new ClipboardItem({{
                "text/plain": new Blob([plain], {{type: "text/plain"}}),
                "text/html": new Blob([rich], {{type: "text/html"}}),
              }})]);
            }} else {{
              await navigator.clipboard.writeText(plain);
            }}
            s.textContent = "コピーしました";
          }} catch (e) {{
            try {{ await navigator.clipboard.writeText(plain); s.textContent = "コピーしました"; }}
            catch (e2) {{ s.textContent = "コピーできませんでした（下の文字を選んでコピーしてください）"; }}
          }}
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


STEPS = ("note での手順: ① タイトルを貼る → ② 無料部分を貼る → ③ 改行して有料部分を貼る → "
         "④ 公開設定で有料・価格を設定し、有料ラインを無料部分と有料部分の境目に置く → "
         "⑤ ハッシュタグ・見出し画像を設定 → ⑥ 公開したら、下の欄に記事の URL を登録")


def _header(row):
    """見出し画像 (key, 理由)。記録が無ければ手動の記事と同じ規則で選ぶ（風・波は分からないので見ない）。"""
    import note_header as nh

    key = row.get("header_image")
    if key in nh.HEADERS and nh.HEADERS[key].exists():
        return key, row.get("header_image_reason") or ""
    return nh.choose(manshu_chance=bool(row.get("is_manshu")), pre_pick=bool(row.get("is_pre")))


def _status_text(row):
    if row.get("note_url"):
        return f"公開済み（{_jst(row.get('published_at'))[11:16]}）"
    return "自動下書き" if row.get("status") == "AUTO_DRAFT" else "手動で確定した記事"


def _article(row, key):
    import note_article as na
    import note_auto
    import note_header as nh

    title, body = str(row.get("title") or ""), str(row.get("article_text") or "")
    free, paid = note_auto.split_by_mark(body)
    st.markdown(f"**{html.escape(str(row.get('venue') or ''))} {row.get('race_no')}R**")
    st.text(f"価格: {int(row.get('price') or 0)}円\n"
            f"締切: {row.get('deadline') or '—'}\n"
            f"記事の生成時刻: {_jst(row.get('created_at'))}\n"
            f"状態: {_status_text(row)}"
            f"・本文: {'Claude' if row.get('article_writer') == 'claude' else '決まった型'}")
    st.caption(STEPS)
    st.markdown("タイトル")
    _copy_block("タイトル", title)
    if paid:
        st.markdown("無料部分（有料ラインより上）")
        _copy_block("無料部分", free, rich=True)
        st.markdown("有料部分（有料ラインより下）")
        _copy_block("有料部分", paid, rich=True)
    else:
        st.markdown("本文（【有料ライン】の目印がありません）")
        _copy_block("本文", body, rich=True)
    with st.expander("本文全体（【有料ライン】の目印つき）", expanded=False):
        _copy_block("本文全体", body)
    st.markdown("ハッシュタグ")
    _copy_block("ハッシュタグ", na.HASHTAGS)

    hk, why = _header(row)
    st.markdown(f"見出し画像: {nh.HEADERS[hk].label}")
    if why:
        st.caption(f"選んだ理由: {why}")
    data = nh.HEADERS[hk].path.read_bytes()
    st.image(data, use_container_width=True)
    st.download_button("💾 見出し画像を保存", data=data, mime="image/jpeg",
                       file_name=nh.file_name(hk, str(row.get("publication_date") or ""),
                                              f"{row.get('venue') or ''}{row.get('race_no')}R"))
    _register(row, key)


def _register(row, key):
    """公開した note 記事の URL を登録する（確定済みの記事・本日N本目・夜のまとめに入る）。"""
    st.markdown("公開した記事の URL を登録")
    if row.get("note_url"):
        st.text(f"登録済み: {row['note_url']}")
    with st.form(f"note_register_{row['race_key']}", clear_on_submit=False):
        url = st.text_input("note 記事の URL（https://note.com/…/n/…）", value=row.get("note_url") or "")
        sent = st.form_submit_button("登録する")
    if not sent:
        return
    try:
        res = _rpc("note_view_register", {"p_key": key, "p_race_key": row["race_key"], "p_note_url": url.strip()})
    except Exception as e:  # noqa: BLE001
        st.error(f"登録できませんでした（{type(e).__name__}）")
        return
    if res in ("registered", "updated"):
        st.success("登録しました。確定済みの記事として、本日N本目・夜のまとめに入ります。" if res == "registered"
                   else "URL を変更しました。")
    elif res == "invalid_url":
        st.error("note 記事の URL（https://note.com/名前/n/記事ID）を入れてください。")
    else:
        st.error("このレースの記事が見つかりませんでした。")


def _list(rows, key, date_iso):
    st.markdown(f"[📊 この日の結果報告記事](?view=note&report={date_iso}&key={key})")
    st.markdown(f"**note 自動下書き（{date_iso}・締切順）**")
    if not rows:
        st.text("自動下書きはまだありません。")
        return
    for r in rows:
        href = f"?view=note&race_key={r['race_key']}&key={key}"
        st.markdown(f"- [{html.escape(str(r.get('venue') or ''))} {r.get('race_no')}R｜締切 {r.get('deadline') or '—'}]"
                    f"({href})（生成 {_jst(r.get('created_at'))[11:16]}・{_status_text(r)}）")


@st.cache_data(ttl=300, show_spinner=False)
def _report(date_iso):
    """結果報告記事（5分だけ覚えておく）。"""
    import note_report
    from result_tracker import supabase_config, supabase_service_config

    url, sb_key = supabase_service_config()
    if not url or not sb_key:
        url, sb_key = supabase_config()
    return note_report.build(url, sb_key, date_iso)


def _report_page(date_iso):
    """1日の締めの結果報告記事。①をそのまま使い、②③は折りたたみ。"""
    rep = _report(date_iso)
    st.markdown(f"**📊 結果報告記事（{date_iso}）**")
    n = len(rep["records"])
    st.text(f"対象: 確定済み（公開済み）の note 記事 {n}本・結果確定 {rep['settled']}本"
            + (f"・結果待ち {len(rep['pending'])}本（"
               + "、".join(f"{r.get('venue')}{r.get('race_no')}R" for r in rep["pending"]) + "）"
               if rep["pending"] else ""))
    if rep["pending"]:
        st.caption("結果待ちのレースは「結果待ち」として載っています。全レースの結果が出てから開き直すと（5分ごとに更新）確定した結果になります。")
    st.caption("note での手順: タイトルと本文を貼って、無料で公開する")
    variants = rep["variants"] or []
    for i, v in enumerate(variants):
        if i == 0:
            st.markdown(f"{v['label']}（おすすめ）")
            st.markdown("タイトル")
            _copy_block("タイトル", v["title"])
            st.markdown("本文")
            _copy_block("本文", v["body"], rich=True)
        else:
            with st.expander(f"別のパターン: {v['label']}", expanded=False):
                _copy_block("タイトル", v["title"])
                _copy_block("本文", v["body"], rich=True)


def render():
    """?view=note のページ。キーが合わなければ何も出さない。"""
    try:
        st.set_page_config(page_title="note記事", layout="centered", initial_sidebar_state="collapsed")
    except Exception:  # noqa: BLE001
        pass
    key, race_key, date_iso = _param("key"), _param("race_key"), _param("date")
    report = _param("report")
    if not key or (race_key and not _RACE_KEY.match(race_key)) or (date_iso and not _DATE.match(date_iso)):
        return
    if report:
        if _DATE.match(report) and _key_ok(key):
            try:
                _report_page(report)
            except Exception as e:  # noqa: BLE001
                st.text(f"結果報告記事を作れませんでした（{type(e).__name__}）。")
        return
    try:
        rows = fetch(key, race_key=race_key or None, date_iso=date_iso or None)
    except Exception:  # noqa: BLE001  キーの有無が分からないように何も出さない
        return
    if race_key:
        if rows:
            _article(rows[0], key)
        return
    # 一覧: キーが違うときも0行なので、キーが正しいと分かったとき（照合用に1回）だけ出す
    if not rows and not _key_ok(key):
        return
    _list(rows, key, date_iso or datetime.now(JST).strftime("%Y-%m-%d"))


def _key_ok(key):
    """一覧が0行のとき、キー自体が正しいか。"""
    try:
        return _rpc("note_view_key_ok", {"p_key": key}) is True
    except Exception:  # noqa: BLE001
        return False
