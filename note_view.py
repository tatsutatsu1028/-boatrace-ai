"""
予想タブの「📝 note記事（有料）」欄（管理者だけ）。

  - 記事に載せる買い目を「モデルのみ予想」（初期値）か「両方の予想平均」から選ぶ。両方の予想平均は
    そのレースで Claude の予想を取ったときだけ選べる
  - 「note記事用に確定」で、本文を Claude（Haiku）に書いてもらってから記事枠を確定する（全て有料300円）。
    Claude が失敗したら今までの決まった型の本文にする
  - 確定後も締切前なら、買い目・本文を保存し直せる（夜のまとめは保存した買い目で的中を判定する）
  - 予想カード画像（記事と同じ買い目・1着確率）を作って保存できる
  - note に貼る用に、本文を「無料部分」（買い目なし）と「有料部分」に分けてコピーできる。
    noteの有料ラインは段落の境目にしか置けないので、コピーは段落ごとに <p> にした形（HTML）と
    空行で区切った文字にして、貼ったあとも段落が分かれたままになるようにする
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

import claude_tab
import claude_usage
import note_article as na
import pick_card
from race_visuals import build_visual_rows, parse_combo
from result_tracker import load_prediction_snapshot, supabase_config

JST = ZoneInfo("Asia/Tokyo")


def _h(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _rpc(name, payload):
    url, key = supabase_config()
    if not url or not key:
        raise RuntimeError("Supabase設定が見つかりません。")
    r = requests.post(f"{url}/rest/v1/rpc/{name}", headers=_h(key), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def _get(path):
    url, key = supabase_config()
    if not url or not key:
        return []
    r = requests.get(f"{url}/rest/v1/{path}", headers=_h(key), timeout=10)
    r.raise_for_status()
    return r.json() or []


def load_publication(race_key):
    try:
        rows = _get(f"note_publications?race_key=eq.{race_key}&select=*")
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=60, show_spinner=False)
def _saved_claude_section(ctx):
    """保存済み予想の "claude"（両方の予想平均の買い目がここにある）。1分だけ覚えておく。"""
    try:
        snap = load_prediction_snapshot(ctx)
        return (json.loads((snap or {}).get("payload_json") or "{}") or {}).get("claude")
    except Exception:  # noqa: BLE001
        return None


def _deadline_passed(d, hhmm):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=JST) <= datetime.now(JST)
    except Exception:  # noqa: BLE001
        return False


def _final_with(final, probs):
    f = final.copy()
    if probs:
        f["p_first"] = pd.to_numeric(f["lane"], errors="coerce").map(lambda ln: probs.get(int(ln), 0.0))
    return f


def _top_combo(tickets):
    for t in tickets:
        if t["group"] == "本線" and parse_combo(t["combo"]):
            return t["combo"]
    return tickets[0]["combo"] if tickets else None


def _copy_buttons(title, body):
    payload = json.dumps({"title": title, "body": body}, ensure_ascii=False).replace("</", "<\\/")
    components.html(
        f"""
        <style>
          body {{ margin:0; }}
          .copy-row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin:2px 0 4px 0; }}
          .copy-row button {{ min-height:44px; padding:6px 4px; cursor:pointer; font-size:13px; border-radius:10px; white-space:nowrap; }}
          #copy-status {{ font-size:13px; color:#9fe7a9; min-height:18px; text-align:center; }}
        </style>
        <div class="copy-row">
          <button id="copy-title">📋 タイトル</button>
          <button id="copy-body">📋 本文</button>
          <button id="copy-tags">📋 ハッシュタグ</button>
        </div>
        <div id="copy-status"></div>
        <script>
        const data = {payload};
        const status = document.getElementById("copy-status");
        async function copyText(value) {{
          try {{
            await navigator.clipboard.writeText(value);
            status.textContent = "コピーしました";
          }} catch (e) {{
            status.textContent = "コピーできませんでした";
          }}
          setTimeout(() => status.textContent = "", 1800);
        }}
        document.getElementById("copy-title").onclick = () => copyText(data.title);
        document.getElementById("copy-body").onclick = () => copyText(data.body);
        document.getElementById("copy-tags").onclick = () => copyText({json.dumps(na.HASHTAGS, ensure_ascii=False)});
        </script>
        """,
        height=76,
    )


def _digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _paste_html(text):
    """空行で区切った段落 → 段落ごとの <p>（段落の中の改行は <br>）。noteに貼ると段落のまま入る。"""
    import html
    import re

    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(ln) for ln in p.splitlines()) + "</p>" for p in paras)


def _split_copy_buttons(free, paid):
    payload = json.dumps({"free": [free, _paste_html(free)], "paid": [paid, _paste_html(paid)]},
                         ensure_ascii=False).replace("</", "<\\/")
    components.html(
        f"""
        <style>
          body {{ margin:0; }}
          .copy-row {{ display:grid; grid-template-columns:repeat(2,1fr); gap:6px; margin:2px 0 4px 0; }}
          .copy-row button {{ min-height:44px; padding:6px 4px; cursor:pointer; font-size:13px; border-radius:10px; white-space:nowrap; }}
          #split-status {{ font-size:13px; color:#9fe7a9; min-height:18px; text-align:center; }}
        </style>
        <div class="copy-row">
          <button id="copy-free">📋 無料部分</button>
          <button id="copy-paid">🔒 有料部分</button>
        </div>
        <div id="split-status"></div>
        <script>
        const parts = {payload};
        const status = document.getElementById("split-status");
        async function copyPart(name, label) {{
          const [plain, html] = parts[name];
          try {{
            if (window.ClipboardItem && navigator.clipboard.write) {{
              await navigator.clipboard.write([new ClipboardItem({{
                "text/plain": new Blob([plain], {{type: "text/plain"}}),
                "text/html": new Blob([html], {{type: "text/html"}}),
              }})]);
            }} else {{
              await navigator.clipboard.writeText(plain);
            }}
            status.textContent = label + "をコピーしました";
          }} catch (e) {{
            try {{
              await navigator.clipboard.writeText(plain);
              status.textContent = label + "をコピーしました";
            }} catch (e2) {{
              status.textContent = "コピーできませんでした";
            }}
          }}
          setTimeout(() => status.textContent = "", 1800);
        }}
        document.getElementById("copy-free").onclick = () => copyPart("free", "無料部分");
        document.getElementById("copy-paid").onclick = () => copyPart("paid", "有料部分");
        </script>
        """,
        height=76,
    )


def _write_claude(ctx, api_key, model, work, final, tickets, probs, rows, label, deadline, source, force=False):
    brief = na.build_brief(work, final, tickets, probs, rows, label, deadline, source)
    with st.spinner("Claudeが記事本文を書いています…"):
        res = na.write_with_claude(api_key, ctx, brief, tickets, model=model or na.ARTICLE_MODEL, force=force)
    if res.get("called_api") and not res.get("usage_logged"):
        res["usage_logged"] = claude_usage.log_call(res, ctx, purpose="note_article")
    st.session_state[f"note_claude_{ctx}_{source}"] = {"tickets": tickets, "result": res}
    return res


def render(ctx, d, venue, rno, deadline, saved_final, model_tickets, work, api_key="", model=None):
    """note記事の欄。確定済みの記事（note_publications の行）を返す（無ければ None）。"""
    st.markdown("#### 📝 note記事（有料）")
    date_iso = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
    label = f"{venue} {rno}R"
    pub = load_publication(ctx)
    closed = _deadline_passed(d, deadline)

    # 載せる買い目
    avg = na.avg_option(None, claude_tab.bundle(ctx)) or na.avg_option(_saved_claude_section(ctx))
    options = ["model"] + (["avg"] if avg else [])
    current = (pub or {}).get("ticket_source") or "model"
    locked = bool(pub) and closed
    if locked:
        source = current
        st.caption(f"記事に載せた買い目: {na.SOURCE_LABELS.get(source, source)}（締切後のため変えません）")
    else:
        source = st.radio("記事に載せる買い目", options, index=options.index(current) if current in options else 0,
                          format_func=lambda m: na.SOURCE_LABELS[m], horizontal=True, key=f"note_source_{ctx}")
        if not avg:
            st.caption("「両方の予想平均」は、Claudeの予想を取ったレース（🧠／⚖️ のボタンで予想）だけ選べます。")
    if source == "avg" and avg:
        tickets, probs = avg
    else:
        source = "model" if not locked else source
        tickets, probs = na.tickets_list(model_tickets), na.p_first_map(saved_final)
    if locked and pub.get("article_tickets"):
        tickets = na.tickets_list(pub["article_tickets"])
    final_used = _final_with(saved_final, probs)
    rows = build_visual_rows(final_used, work, top_combo=_top_combo(tickets))

    # 本文: Claude の本文（このセッションで書いたもの）→ 保存した本文 → 決まった型
    gen = st.session_state.get(f"note_claude_{ctx}_{source}")
    res = gen["result"] if gen and na.same_tickets(gen["tickets"], tickets) else None
    saved_same = bool(pub) and (pub.get("ticket_source") or "model") == source and pub.get("article_tickets") \
        and na.same_tickets(pub["article_tickets"], tickets) and pub.get("article_text")
    if locked and pub.get("article_text"):
        body, writer = pub["article_text"], pub.get("article_writer") or "template"
    elif res and res.get("status") == "ok":
        body, writer = res["body"], "claude"
    elif saved_same and not res:
        body, writer = pub["article_text"], pub.get("article_writer") or "template"
    else:
        body, writer = na.template_article(tickets), "template"
    if res and res.get("status") != "ok" and not locked:
        st.warning(f"Claudeで本文を作れなかったため、決まった型の本文にしています（{res.get('error')}）。")

    if pub:
        st.success(f"🔒 本日{int(pub.get('sequence_no') or 0)}本目 → 有料予想 {int(pub.get('price') or 0)}円（確定済み）"
                   if pub.get("publication_type") == "PAID" else
                   f"✅ 本日{int(pub.get('sequence_no') or 0)}本目 → 無料予想（確定済み・以前のルール）")
        changed = (not saved_same) or (writer == "claude" and body != pub.get("article_text"))
        if not closed and changed:
            st.info("記事の買い目・本文が、確定したときと違います。夜のまとめの的中判定にも使うので保存し直してください。")
            if st.button("💾 記事の買い目・本文を保存し直す", key=f"note_update_{ctx}"):
                try:
                    _rpc("update_note_publication_article", {
                        "p_race_key": ctx, "p_ticket_source": source, "p_article_tickets": tickets,
                        "p_title": na.article_title(venue, rno, deadline), "p_article_text": body,
                        "p_article_writer": writer})
                    st.success("保存し直しました。")
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error("保存に失敗しました。")
                    st.code(str(e))
    else:
        st.caption("まだ記事枠は確定していません。noteは全て有料（300円）として記録します。"
                   "確定するときに、本文をClaudeが書きます（失敗したときは決まった型）。")
        if st.button("📝 note記事用に確定（有料）", key=f"reserve_note_{ctx}", disabled=closed):
            if not res and api_key:
                res = _write_claude(ctx, api_key, model, work, final_used, tickets, probs, rows, label, deadline,
                                    source)
            if res and res.get("status") == "ok":
                body, writer = res["body"], "claude"
            else:
                body, writer = na.template_article(tickets), "template"
            p1_lane = max(probs, key=lambda k: probs[k]) if probs else None
            try:
                _rpc("reserve_note_publication_v2", {
                    "p_race_key": ctx, "p_publication_date": date_iso, "p_venue": venue, "p_race_no": int(rno),
                    "p_trigger_reason": "オーナー判断", "p_p1_lane": p1_lane,
                    "p_p1_prob": float(probs[p1_lane]) if p1_lane else None,
                    "p_title": f"{venue} {rno}R｜締切 {deadline}" if deadline else f"{venue} {rno}R",
                    "p_article_text": body, "p_ticket_source": source, "p_article_tickets": tickets,
                    "p_article_writer": writer,
                })
                st.success("記事枠を確定しました（有料）。")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error("記事枠の確定に失敗しました。")
                st.code(str(e))
        if closed:
            st.caption("締切後のため、記事枠は確定できません。")

    if not locked and api_key and st.button("🧠 Claudeで本文を作り直す" if res else "🧠 Claudeで本文を作る",
                                            key=f"note_claude_btn_{ctx}_{source}"):
        _write_claude(ctx, api_key, model, work, final_used, tickets, probs, rows, label, deadline, source,
                      force=bool(res))
        st.rerun()
    if res and res.get("status") == "ok" and res.get("cost"):
        st.caption(f"本文: Claude（{res.get('served_model') or res.get('model')}）約{res.get('seconds', 0)}秒"
                   f" ／ 費用 約{res['cost']['jpy']:.2f}円")
    elif writer == "template":
        st.caption("本文: 決まった型")

    title = st.text_input("noteタイトル", value=na.article_title(venue, rno, deadline), key=f"note_title_{ctx}")
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]
    body = st.text_area("note本文（そのままコピー用）", value=body, height=420, key=f"note_body_{ctx}_{digest}")
    _copy_buttons(title, body)

    # note に貼る用: 無料部分（買い目なし）と有料部分
    free, paid = na.split_article(body, label, deadline)
    st.markdown("##### ✂️ noteに貼る用（無料部分／有料部分）")
    st.caption("noteでの手順: ① 無料部分を貼る → ② 改行する → ③ 有料部分を貼る → "
               "④ 2つの境目（「ここから先は有料部分です」の下）に有料ラインを置く")
    c1, c2 = st.columns(2)
    with c1:
        free = st.text_area("無料部分（買い目なし）", value=free, height=260, key=f"note_free_{ctx}_{_digest(body)}")
    with c2:
        paid = st.text_area("有料部分（買い目・詳しい展開）", value=paid, height=260,
                            key=f"note_paid_{ctx}_{_digest(body)}")
    _split_copy_buttons(free, paid)

    # 予想カード画像
    with st.expander("🖼 予想カード画像", expanded=False):
        if not pick_card.has_japanese_font():
            st.caption("日本語フォントが見つからないため、文字が正しく出ないことがあります（packages.txt を確認）。")
        fav = None
        if probs:
            lane = max(probs, key=lambda k: probs[k])
            fav = (lane, next((r["name"] for r in rows if r["lane"] == lane), ""))
        card_key = f"note_card_{ctx}_{source}_{hashlib.sha1(json.dumps(tickets).encode()).hexdigest()[:8]}"
        if st.button("🖼 カード画像を作る", key=f"{card_key}_btn"):
            st.session_state[card_key] = pick_card.render_card(
                venue, rno, deadline, d.strftime("%Y/%m/%d") if hasattr(d, "strftime") else date_iso,
                tickets, probs, rows, na.SOURCE_LABELS.get(source, ""), favorite=fav)
        png = st.session_state.get(card_key)
        if png:
            st.image(png, use_container_width=True)
            st.download_button("💾 画像を保存", data=png, file_name=f"{date_iso}_{venue}{rno}R.png",
                               mime="image/png", key=f"{card_key}_dl", use_container_width=True)
    return pub
