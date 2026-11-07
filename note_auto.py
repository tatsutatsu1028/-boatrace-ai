"""
note 有料記事の自動下書き（締切前の Discord 通知と同じタイミング）。

展示後の自動予想（auto_post_predict.py・GitHub Actions）が、展示後の予想を保存して（万舟チャンスは中穴〜万舟狙いの買い目も
作り直して）すぐに、そのレースの記事を作って note_publications に status = AUTO_DRAFT で保存する。
締切前の通知（Supabase の notify_due_races）は、記事ができる（auto_post_log.note_status が入る）のを待ってから送り、
記事表示ページ（app.py の ?view=note。秘密のキー付き）の URL か「記事生成失敗」を載せる
（supabase/migrations/20261107000000_note_auto_draft.sql）。

  - 対象: 通知するレース（🌅 展示前厳選10・💥 万舟チャンス）。アプリの設定 app_settings.note_auto_enabled が ON のときだけ
  - 買い目: app_settings.note_auto_ticket_source（model = モデルのみ予想（既定）/ avg = 両方の予想平均）。
    両方の予想平均は、ここで Claude の読み（claude_reader.read_race）を取り、予想タブの「両方の予想平均」と同じ手順で作る。
    取れなければモデルのみ予想にする（理由を残す）
  - 本文: 手動の記事と同じ note_article（Claude Haiku・フレンドリーな文体・買い目と予想展開）。
    無料部分と有料部分の境目に「【有料ライン】」の行を入れる（note_article.split_article と同じ分け方）
  - Claude が失敗したときは、手動と同じ決まった型（買い目だけ）で保存し、note_status = template（通知に「記事生成失敗」）
  - アプリで「note記事用に確定」した記事（手動の記事）が既にあれば書かない（note_status = manual）

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


def ticket_source(settings):
    return "avg" if (settings or {}).get("note_auto_ticket_source") == "avg" else "model"


def with_paywall_mark(body, race_label="", deadline=""):
    """本文 → 無料部分・【有料ライン】・有料部分 をつないだ記事の本文（段落は空行で区切る）。"""
    free, paid = na.split_article(body, race_label, deadline)
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
    if not enabled(settings):
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
        res = na.write_with_claude(api_key, t["race_key"], brief, rows_t,
                                   model=article_model or na.ARTICLE_MODEL)
        if res.get("called_api"):
            _log_usage(sb, res, t["race_key"], USAGE_PURPOSE)
        if res.get("status") == "ok":
            body, writer = res["body"], "claude"
        else:
            body, writer = na.template_article(rows_t), "template"
            notes.insert(0, f"Claude: {res.get('error') or '不明'}")
        out["writer"] = writer
        p1 = max(probs, key=lambda k: probs[k]) if probs else None
        saved = sb("POST", "rpc/note_auto_save_draft", json={
            "p_race_key": t["race_key"], "p_publication_date": t["race_date"], "p_venue": venue,
            "p_race_no": rno, "p_title": na.article_title(venue, rno, deadline),
            "p_article_text": with_paywall_mark(body, label, deadline), "p_ticket_source": source,
            "p_article_tickets": rows_t, "p_article_writer": writer, "p_p1_lane": p1,
            "p_p1_prob": float(probs[p1]) if p1 else None, "p_price": na.PRICE_YEN,
        }).json()
        if saved == "manual":
            out["status"] = "manual"
            notes.insert(0, "手動で確定した記事があるため自動下書きは作りませんでした")
        else:
            out["status"] = "ok" if writer == "claude" else "template"
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
