"""
note 有料記事の自動下書き（締切前の Discord 通知と同じタイミング）。

展示後の自動予想（auto_post_predict.py・GitHub Actions）が、展示後の予想を保存して（万舟チャンスは中穴〜万舟狙いの買い目も
作り直して）すぐに、そのレースの記事を作って note_publications に status = AUTO_DRAFT で保存する。
締切前の通知（Supabase の notify_due_races）は、記事ができる（auto_post_log.note_status が入る）のを待ってから送り、
記事表示ページ（app.py の ?view=note。秘密のキー付き）の URL か「記事生成失敗」を載せる
（supabase/migrations/20261107000000_note_auto_draft.sql）。

  - 対象: アプリの設定 app_settings.note_auto_enabled が ON のとき、厳選10（朝一厳選）のレース。設定 note_auto_scope が
    「厳選＋万舟」（pre_manshu）なら万舟チャンスのレースも（既定は「厳選のみ」= pre）
  - 買い目: app_settings.note_auto_ticket_source（model = モデルのみ予想（既定）/ avg = 両方の予想平均）。
    両方の予想平均は、ここで Claude の読み（claude_reader.read_race）を取り、予想タブの「両方の予想平均」と同じ手順で作る。
    取れなければモデルのみ予想にする（理由を残す）
  - 本文: 手動の記事と同じ note_article（Claude Haiku・フレンドリーな文体・買い目と予想展開）。
    無料部分と有料部分の境目に「【有料ライン】」の行を入れる（note_article.split_article と同じ分け方）
  - Claude が失敗したときは、手動と同じ決まった型（買い目だけ）で保存し、note_status = template（通知に「記事生成失敗」）
  - アプリで「note記事用に確定」した記事（手動の記事）が既にあれば書かない（note_status = manual）
  - 見出し画像も手動の記事と同じ規則（note_header.py）で選んで記録する（note_publications.header_image*）

Streamlit には依存しない（tests で確かめられるように）。
"""

from __future__ import annotations

import os
import time

import note_article as na

STATUS = "AUTO_DRAFT"
PAYWALL_MARK = "【有料ライン】"
USAGE_PURPOSE = "note_article_auto"
READ_PURPOSE = "note_article_auto_read"


def enabled(settings):
    return bool((settings or {}).get("note_auto_enabled"))


SCOPES = ("pre", "pre_manshu")
SCOPE_LABELS = {"pre": "厳選のみ", "pre_manshu": "厳選＋万舟"}


def scope(settings):
    """記事を作る対象: pre = 厳選10（朝一厳選）だけ（既定）/ pre_manshu = 厳選10＋万舟チャンス。"""
    return "pre_manshu" if (settings or {}).get("note_auto_scope") == "pre_manshu" else "pre"


def applies(settings, t):
    """このレースの記事を作るか。ON で、厳選10のレース（対象が「厳選＋万舟」なら万舟チャンスのレースも）。"""
    sources = (t or {}).get("sources") or []
    return enabled(settings) and ("pre" in sources or (scope(settings) == "pre_manshu" and "manshu" in sources))


def ticket_source(settings):
    return "avg" if (settings or {}).get("note_auto_ticket_source") == "avg" else "model"


def with_paywall_mark(body, race_label="", deadline="", paywall_lead=None):
    """本文 → 無料部分・【有料ライン】・有料部分 をつないだ記事の本文（段落は空行で区切る）。"""
    free, paid = na.split_article(body, race_label, deadline, paywall_lead)
    return f"{free}\n\n{PAYWALL_MARK}\n\n{paid}"


def split_by_mark(text):
    """【有料ライン】の行で (無料部分, 有料部分) に分ける。目印が無ければ (全部, "")。"""
    lines = str(text or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == PAYWALL_MARK:
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i + 1:]).strip()
    return str(text or "").strip(), ""


def _top_combo(tickets):
    from race_visuals import parse_combo

    for t in tickets:
        if t["group"] == "本線" and parse_combo(t["combo"]):
            return t["combo"]
    return tickets[0]["combo"] if tickets else None


def _final_with(final, probs):
    import pandas as pd

    f = final.copy()
    if probs:
        f["p_first"] = pd.to_numeric(f["lane"], errors="coerce").map(lambda ln: probs.get(int(ln), 0.0))
    return f


def _log_usage(sb, res, race_key, purpose):
    import claude_reader as cr

    row = cr.usage_row(res, race_key, purpose)
    if row is None:
        return
    try:
        sb("POST", "claude_usage", prefer="return=minimal", json=row)
    except Exception as e:  # noqa: BLE001  記録に失敗しても記事は止めない
        print("[NOTE_AUTO] claude_usage の記録に失敗:", type(e).__name__, flush=True)


def avg_prediction(sb, t, work, final, odds, runtime, api_key, read_model=None):
    """「両方の予想平均」の (買い目 [{combo, group}], {lane: 1着確率})。作れなければ (None, 理由)。"""
    if work is None or not len(work):
        return None, "レースの表が無いため"
    if not api_key:
        return None, "ANTHROPIC_API_KEY が未設定"
    import claude_reader as cr
    from claude_tab import build_tickets
    from model_only import complete_adaptive_tickets
    from prediction import trifecta

    label = f"{t.get('venue') or ''} {t['race_no']}R"
    reading = cr.read_race(api_key, t["race_key"], work, final, model=read_model, race_label=label)
    _log_usage(sb, reading, t["race_key"], READ_PURPOSE)
    if reading.get("status") != "ok":
        return None, f"Claudeの読みを取れなかったため（{reading.get('error')}）"
    avg_tri = cr.average_tri(trifecta(final), reading["tickets"])
    avg_final = cr.final_from_tri(final, avg_tri)
    tickets, _ = build_tickets(avg_final, work, odds, runtime, complete_adaptive_tickets, tri=avg_tri)
    return (na.tickets_list(tickets), {int(r.lane): float(r.p_first) for r in avg_final.itertuples()}), None


def choose_header(t, work=None):
    """手動の記事と同じ規則（note_header.choose）で見出し画像を選ぶ (key, 理由)。買い目は中穴〜万舟狙いではない。"""
    import note_header as nh

    wind, wave = nh.water(work)
    sources = t.get("sources") or []
    return nh.choose(manshu_chance="manshu" in sources, with_aim=False, wind=wind, wave=wave,
                     pre_pick="pre" in sources)


def record_header(sb, t, work=None):
    """自動下書きの見出し画像を選んで記録する（set_note_header_image）。失敗しても記事は止めない。戻り値は key（失敗は None）。"""
    try:
        key, why = choose_header(t, work)
        sb("POST", "rpc/set_note_header_image",
           json={"p_race_key": t["race_key"], "p_image": key, "p_auto": key, "p_reason": why[:200]})
        return key
    except Exception as e:  # noqa: BLE001
        print("[NOTE_AUTO] 見出し画像の記録に失敗:", type(e).__name__, flush=True)
        return None


# ---------------------------------------------------------------
# 万舟チャンスのレース: 万舟チャンスの買い目（展示後に作り直したもの）と通常の予想（本線・抑え）の2種類を載せる
# ---------------------------------------------------------------
MANSHU_TITLE_HEAD = "【万舟チャンス】"
MANSHU_INTRO = "💥 このレースは「万舟チャンス」に選ばれたレースです。モデルが、3連単100倍以上の高配当（万舟）が出やすいと見ています。"
MANSHU_AIM_HEAD = "■ 万舟チャンスの買い目（中穴〜万舟狙い）"
MANSHU_NO_AIM = ("■ 万舟チャンスの買い目\n展示後に万舟チャンスの買い目を作り直せなかったため、今回は載せていません。"
                 "通常の予想の買い目をお届けします。")
MANSHU_MODEL_HEAD = "■ 通常の予想（本線・抑え）"
OVERLAP_NOTE = "※重複：万舟チャンスの買い目と通常の予想の両方に入っている組です。"
PAYWALL_MANSHU = "ここから先は有料部分です。展示後の詳しい予想展開と、万舟チャンスの買い目（中穴〜万舟狙い）・通常の予想（本線・抑え）の2種類の買い目をお届けします。"
PAYWALL_MANSHU_NO_AIM = "ここから先は有料部分です。展示後の詳しい予想展開と、通常の予想（本線・抑え）の買い目をお届けします。"
MANSHU_PROMPT_VERSION = "note-article-manshu-v1"
MANSHU_KEYS = ("lead", "development", "aim_comment", "ticket_comment")
MANSHU_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in MANSHU_KEYS},
    "required": list(MANSHU_KEYS),
    "additionalProperties": False,
}
MANSHU_SYSTEM = """あなたはボートレース予想の note 記事を書くライターです。展示航走のあとに出す有料記事の本文を書きます。
このレースは「万舟チャンス」（モデルが、3連単100倍以上の高配当が出やすいと見ているレース）に選ばれています。
渡されるのは、1レース分のデータ（出走表・展示・気象）、各艇の1着確率、想定ST・1マークの想定の並び、
「万舟チャンス」の情報（万舟になる確率・本命と本命の1着確率・頭本命（1着軸）・展示後の注意）、
記事に載せる2種類の3連単の買い目（万舟チャンスの買い目＝中穴・万舟、通常の予想＝本線・抑え）です。

書くもの:
- lead: 読者へのあいさつと、このレースの見どころ（60〜100字）。万舟チャンスのレースであることに触れる
- development: 展示後の予想展開。スタート → 1マーク → 2着・3着争いの順に（200〜350字）
- aim_comment: 万舟チャンスの買い目の解説（2〜3文）。なぜ荒れそうと見ているかを、本命の1着確率から見た信頼度や、
  展示で気になった点など、渡されたデータにあることで書く。頭本命（1着軸）が渡されていれば、その艇に必ず触れる
- ticket_comment: 通常の予想（本線・抑え）の組み立ての考え方（80〜150字）

守ること:
- フレンドリーで読みやすい口調（です・ます）。絵文字は使ってもよいが多くしない（全体で3つまで）。
- 渡されたデータに書かれていることだけを使う。データに無いこと（選手の評判・過去の対戦・記者コメント・
  ピットの様子・オッズ・モーターの整備内容など）は書かない。値が空欄の項目には触れない。
- 数値を挙げるときは入力の値をそのまま使う。
- 断定しすぎない（「〜と見ます」「〜が有力」「〜に期待」など）。「絶対」「鉄板」「確実」「間違いない」は使わない。
- 3連単の組番（1-2-3 の形）を書くときは、渡された買い目に入っている組番だけにする。
  組番を書いてよいのは aim_comment と ticket_comment だけ（lead・development は無料で読める部分に載るため、組番は書かない）。
- development の最初の1文は、組番や本命を明かさない「展開の入り」（スタートの見立てなど）にする。
- 買い目の一覧そのものは書かない（記事の後ろにこちらで付ける）。
- 万舟チャンスの買い目は高配当狙いで当たる確率は低めであることに触れてよい。当たりやすいとは書かない。"""


def manshu_price(settings):
    """万舟チャンスの記事の価格（設定タブ。初期値は厳選と同じ300円）。"""
    try:
        v = int((settings or {}).get("note_auto_price_manshu") or na.PRICE_YEN)
    except (TypeError, ValueError):
        v = na.PRICE_YEN
    return v if 100 <= v <= 50000 else na.PRICE_YEN


def load_manshu(sb, t):
    """このレースの万舟チャンスの行（manshu_chance_picks）。万舟チャンスでなければ None。"""
    if "manshu" not in ((t or {}).get("sources") or []):
        return None
    rows = sb("GET", f"manshu_chance_picks?race_key=eq.{t['race_key']}&select=aim_tickets,aim_saved_at,aim_source,"
                     "post_saved_at,post_head_axis_lane,head_axis_lane,manshu_prob,post_manshu_prob,"
                     "post_flag_reasons&limit=1").json() or []
    return rows[0] if rows else None


def _ts(v):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def rebuilt_aim(mark):
    """展示後に作り直した万舟チャンスの買い目 [{combo, group: 中穴/万舟}]。作り直していなければ None。
    「作り直した」は、買い目の保存が展示後の予想の保存より後（締切前の通知の判定と同じ）。"""
    aim = na.aim_option(mark)
    a, p = _ts((mark or {}).get("aim_saved_at")), _ts((mark or {}).get("post_saved_at"))
    if not aim or a is None or p is None or a < p:
        return None
    return aim


def _combos(tickets):
    return [x["combo"] for x in na.tickets_list(tickets)]


def _block(groups, overlap):
    """[(見出し, 組番)] → 買い目の欄。重なる組は1組ずつ「※重複」を付け、それ以外は厳選の記事と同じくまとめる。"""
    from threads_poster import compact_combos

    lines = []
    for head, combos in groups:
        plain = [c for c in combos if c not in overlap]
        dup = [f"{c} ※重複" for c in combos if c in overlap]
        lines += ([""] if lines else []) + [f"【{head}】"] + ((compact_combos(plain) + dup) or ["なし"])
    return "\n".join(lines)


def compose_manshu(parts, aim, model):
    """万舟チャンスの記事の本文。parts は Claude の文章（無ければ空の dict = 決まった型）。aim が None なら万舟チャンスの欄を省く。"""
    from threads_poster import split_main_cover

    parts = parts or {}
    model_main, model_cover = split_main_cover(model)
    overlap = set(_combos(aim)) & set(_combos(model)) if aim else set()
    paras = [MANSHU_INTRO]
    if str(parts.get("lead") or "").strip():
        paras.append(parts["lead"].strip())
    if str(parts.get("development") or "").strip():
        paras.append("■ 展示後の予想展開\n" + parts["development"].strip())
    if aim:
        comment = str(parts.get("aim_comment") or "").strip()
        mid, high = na.aim_groups(aim)
        paras += [MANSHU_AIM_HEAD + ("\n" + comment if comment else ""),
                  _block([(na.AIM_GROUPS["中穴"], mid), (na.AIM_GROUPS["万舟"], high)], overlap), na.AIM_NOTE]
    else:
        paras.append(MANSHU_NO_AIM)
    comment = str(parts.get("ticket_comment") or "").strip()
    paras += [MANSHU_MODEL_HEAD + ("\n" + comment if comment else ""),
              _block([("本線買い目", model_main), ("抑え買い目", model_cover)], overlap)]
    if overlap:
        paras.append(OVERLAP_NOTE)
    paras.append("\n".join(na.DISCLAIMER))
    return "\n\n".join(paras)


def manshu_brief(brief, mark, aim, model, probs):
    """厳選と同じ brief に、万舟チャンスの情報と2種類の買い目を足す。"""
    from threads_poster import split_main_cover

    b = dict(brief)
    mid, high = na.aim_groups(aim)
    main, cover = split_main_cover(model)
    b["載せる買い目"] = {"万舟チャンスの買い目": {na.AIM_GROUPS["中穴"]: mid, na.AIM_GROUPS["万舟"]: high},
                     "通常の予想": {"本線": main, "抑え": cover}}
    fav = max(probs, key=lambda k: probs[k]) if probs else None
    head = (mark or {}).get("post_head_axis_lane") or (mark or {}).get("head_axis_lane")
    mp = (mark or {}).get("post_manshu_prob") or (mark or {}).get("manshu_prob")
    b["万舟チャンス"] = {
        "万舟になる確率": round(float(mp), 3) if mp is not None else None,
        "本命": f"{fav}号艇" if fav else None,
        "本命の1着確率": round(float(probs[fav]), 3) if fav else None,
        "頭本命（1着軸）": f"{int(head)}号艇" if head else None,
        "展示後の注意": "、".join((mark or {}).get("post_flag_reasons") or []) or None,
    }
    return b


def generate(sb, t, final, tickets, work=None, odds=None, settings=None, runtime=None, api_key="",
             article_model=None, read_model=None):
    """
    1レースの記事を作って保存する。例外は出さない。
      t: auto_post_predict の対象（race_key, race_date, venue, race_no, deadline）
      final・tickets・work: 保存した展示後の予想（final の p_first、買い目、レースの表）
    戻り値 {"status": ok/template/manual/failed/off, "reason", "seconds", "source", "writer"}
    """
    t0 = time.time()
    out = {"status": "failed", "reason": None, "source": None, "writer": None}
    if not applies(settings, t):
        out.update(status="off", seconds=0.0)
        return out
    try:
        from race_visuals import build_visual_rows

        source = ticket_source(settings)
        notes = []
        rows_t, probs = na.tickets_list(tickets), na.p_first_map(final)
        if source == "avg":
            try:
                avg, why = avg_prediction(sb, t, work, final, odds, runtime or {}, api_key, read_model)
            except Exception as e:  # noqa: BLE001
                avg, why = None, f"エラー（{type(e).__name__}）"
            if avg:
                rows_t, probs = avg
            else:
                source = "model"
                notes.append(f"両方の予想平均を作れずモデルのみ予想にしました（{why}）")
        out["source"] = source
        if not rows_t:
            raise RuntimeError("買い目がありません")
        venue, rno, deadline = t.get("venue") or "", int(t["race_no"]), str(t.get("deadline") or "")
        label = f"{venue} {rno}R"
        final_used = _final_with(final, probs)
        visual = build_visual_rows(final_used, work, top_combo=_top_combo(rows_t))
        brief = na.build_brief(work, final_used, rows_t, probs, visual, label, deadline, source)
        model = article_model or na.ARTICLE_MODEL
        mark = load_manshu(sb, t)
        title, price, paywall, article_tickets = na.article_title(venue, rno, deadline), na.PRICE_YEN, None, rows_t
        if mark is None:
            # 厳選10の記事（今までと同じ）
            res = na.write_with_claude(api_key, t["race_key"], brief, rows_t, model=model)
            template = na.template_article
        else:
            # 万舟チャンスの記事: 万舟チャンスの買い目（展示後に作り直したもの）と通常の予想の2種類
            aim = rebuilt_aim(mark)
            title, price = MANSHU_TITLE_HEAD + title, manshu_price(settings)
            paywall = PAYWALL_MANSHU if aim else PAYWALL_MANSHU_NO_AIM
            article_tickets = (aim or []) + rows_t
            out["manshu_aim"] = bool(aim)
            if not aim:
                notes.append("万舟チャンスの買い目を展示後に作り直せなかったため、通常の予想だけを載せました")
            res = na.write_with_claude(
                api_key, t["race_key"], manshu_brief(brief, mark, aim, rows_t, probs) if aim else brief,
                article_tickets, model=model,
                **({"system": MANSHU_SYSTEM, "schema": MANSHU_SCHEMA, "keys": MANSHU_KEYS,
                    "prompt_version": MANSHU_PROMPT_VERSION} if aim else {}),
                compose_fn=lambda parts: compose_manshu(parts, aim, rows_t))
            template = lambda _t: compose_manshu({}, aim, rows_t)  # noqa: E731
        if res.get("called_api"):
            _log_usage(sb, res, t["race_key"], USAGE_PURPOSE)
        if res.get("status") == "ok":
            body, writer = res["body"], "claude"
        else:
            body, writer = template(rows_t), "template"
            notes.insert(0, f"Claude: {res.get('error') or '不明'}")
        out["writer"] = writer
        p1 = max(probs, key=lambda k: probs[k]) if probs else None
        saved = sb("POST", "rpc/note_auto_save_draft", json={
            "p_race_key": t["race_key"], "p_publication_date": t["race_date"], "p_venue": venue,
            "p_race_no": rno, "p_title": title,
            "p_article_text": with_paywall_mark(body, label, deadline, paywall), "p_ticket_source": source,
            "p_article_tickets": article_tickets, "p_article_writer": writer, "p_p1_lane": p1,
            "p_p1_prob": float(probs[p1]) if p1 else None, "p_price": price,
        }).json()
        if saved == "manual":
            out["status"] = "manual"
            notes.insert(0, "手動で確定した記事があるため自動下書きは作りませんでした")
        else:
            out["status"] = "ok" if writer == "claude" else "template"
            out["header"] = record_header(sb, t, work)
        out["reason"] = "・".join(notes) or None
    except Exception as e:  # noqa: BLE001  記事が作れなくても予想・通知は止めない
        out.update(status="failed", reason=f"{type(e).__name__}: {str(e)[:150]}")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def generate_from_env(sb, t, final, tickets, work=None, odds=None, settings=None, runtime=None):
    """GitHub Actions の環境変数（ANTHROPIC_API_KEY・CLAUDE_ARTICLE_MODEL・CLAUDE_READ_MODEL）で generate を呼ぶ。"""
    return generate(
        sb, t, final, tickets, work=work, odds=odds, settings=settings, runtime=runtime,
        api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        article_model=os.environ.get("CLAUDE_ARTICLE_MODEL", "").strip() or None,
        read_model=os.environ.get("CLAUDE_READ_MODEL", "").strip() or None,
    )
