"""
「💥 万舟チャンス」「🌅 展示前厳選10」の一覧の各レースに付ける「📝 note記事」「🧵 スレッズ投稿」（管理者だけ）。

  - ボタンを押すと、そのレースの欄の中に予想タブと同じ欄（note_view.render・threads_view.render）を開く。
    スマホで押しやすいよう、2つのボタンは横幅いっぱいに並べる。開いていない欄は読み込まない
  - 使う予想: 展示後の予想（自動予想・手動予想）があればそれ、無ければ朝の予想（race_posts.choose_prediction）。
    どちらを使ったかを欄の頭に出す
  - 万舟チャンスのレースは、記事に載せる買い目を最初から「中穴〜万舟狙い」にしておく。中穴〜万舟狙いの買い目は
    最後に作り直した時刻を出し、その場でオッズを取り直して作り直せる
  - 投稿済み（note記事の確定・スレッズの投稿・朝9:00の自動投稿）は、ボタンの上に時刻つきで出す
"""

from __future__ import annotations

import streamlit as st

import race_posts


def _secret_text(name):
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


@st.cache_data(ttl=60, show_spinner=False)
def _snapshot(race_key):
    from result_tracker import load_prediction_snapshot

    try:
        return load_prediction_snapshot(race_key)
    except Exception:  # noqa: BLE001
        return None


def _toggle(name):
    st.session_state[name] = not st.session_state.get(name, False)


def _aim_block(d, mark, key):
    """中穴〜万舟狙いの買い目: 最後に作り直した時刻と、その場で作り直すボタン。"""
    import manshu
    import manshu_view

    rk = mark["race_key"]
    if mark.get("aim_tickets"):
        st.caption(f"{manshu.AIM_LABEL}: {manshu_view.aim_source_text(mark).strip('（）')}"
                   + (f"　最後に作り直した時刻 {manshu_view.jst_hhmm(mark.get('aim_saved_at'))}"
                      if mark.get("aim_saved_at") else ""))
    elif mark.get("aim_error"):
        st.caption(f"{manshu.AIM_LABEL}: {manshu_view.aim_error_text(mark)}")
    else:
        st.caption(f"{manshu.AIM_LABEL}: まだ作っていません。")
    if manshu_view._deadline_passed(d, mark.get("deadline")):
        return
    if st.button("🔄 オッズを更新して中穴〜万舟狙いの買い目を作り直す", key=f"{key}_aim_{rk}",
                 use_container_width=True):
        if manshu_view.rebuild_aim(mark):
            st.rerun()


def render(d, r, list_key):
    """一覧の1レース分（render_card の中）。r は pre_exhibition_picks / manshu_chance_picks の行。"""
    import manshu_view
    import note_view
    import threads_view

    rk = r["race_key"]
    status = threads_view.race_status(rk, note_view.load_day_publications(d.isoformat()))
    for line in race_posts.status_lines(status):
        st.caption(line)

    note_key, threads_key = f"{list_key}_pub_note_{rk}", f"{list_key}_pub_threads_{rk}"
    note_open, threads_open = st.session_state.get(note_key, False), st.session_state.get(threads_key, False)
    c1, c2 = st.columns(2)
    with c1:
        st.button("📝 note記事" + ("（閉じる）" if note_open else ""), key=f"{note_key}_btn", on_click=_toggle,
                  args=(note_key,), use_container_width=True, type="primary" if note_open else "secondary")
    with c2:
        st.button("🧵 スレッズ投稿" + ("（閉じる）" if threads_open else ""), key=f"{threads_key}_btn",
                  on_click=_toggle, args=(threads_key,), use_container_width=True,
                  type="primary" if threads_open else "secondary")
    if not (note_open or threads_open):
        return

    snap = _snapshot(rk)
    pred = race_posts.choose_prediction(snap, r)
    if pred is None:
        st.warning("使える予想がありません（展示後の予想も朝の予想も読めませんでした）。")
        return
    saved = race_posts.jst_hhmm(pred["saved_at"])
    st.info(f"使う予想: **{pred['label']}**" + (f"（{saved} 保存）" if saved else ""))
    if pred["kind"] == "morning":
        st.caption("展示後の予想がまだ無いため、朝の予想（展示前の1着確率と買い目）を使っています。"
                   "予想タブでこのレースを予想するか、展示後の自動予想が終わると、そちらに切り替わります。")

    mark = manshu_view.marks_by_key(d).get(rk)
    if mark:
        _aim_block(d, mark, f"{list_key}_pub")

    venue, rno, deadline = r["venue"], int(r["race_no"]), str(r.get("deadline") or "").strip()
    prefix = f"{list_key}_pub_"
    pub = None
    if note_open:
        with st.container(border=True):
            try:
                pub = note_view.render(
                    rk, d, venue, rno, deadline, pred["final"], pred["tickets"], pred["work"],
                    api_key=_secret_text("ANTHROPIC_API_KEY"), model=_secret_text("CLAUDE_ARTICLE_MODEL") or None,
                    key_prefix=prefix, default_source="aim" if mark else None)
            except Exception as e:  # noqa: BLE001
                st.caption(f"note記事の欄を表示できませんでした: {type(e).__name__}: {e}")
    if threads_open:
        with st.container(border=True):
            st.markdown("#### 🧵 スレッズに投稿")
            cfg = threads_view.load_config()
            if not cfg:
                st.caption("スレッズ連携が未設定です（設定タブで登録してください）。")
                return
            if pub is None:
                pub = note_view.load_publication(rk)
            # 文章の初期値: note の欄で選んでいる買い目（開いていなければ、万舟チャンスは中穴〜万舟狙い）
            chosen = st.session_state.get(f"{prefix}note_source_{rk}") or (pub or {}).get("ticket_source") \
                or ("aim" if mark else "model")
            threads_view.render(rk, d, venue, rno, deadline, pred["final"], pred["tickets"], pub, cfg,
                                is_manshu=bool(mark), key_prefix=prefix, screen="list", prediction=pred["label"],
                                default_style=race_posts.style_for(chosen))
