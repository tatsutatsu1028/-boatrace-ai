"""
note記事・スレッズ投稿に使う予想と、二重投稿を防ぐための状態（Streamlit を使わない部分。tests で確かめる）。

予想タブと「💥 万舟チャンス」「🌅 展示前厳選10」の一覧（管理者だけ）で同じ関数を使い、
記事・投稿の中身（文章・画像・保存の形）をそろえる。

  - frames_from_snapshot: 保存された予想（prediction_snapshots の行）→ (final, tickets, work)。
    予想タブの「保存された最新の予想」と同じ戻し方
  - morning_frames      : 朝の予想（pre_exhibition_picks / manshu_chance_picks の行）→ (final, tickets, None)
  - choose_prediction   : 一覧で使う予想。展示後の予想（自動予想・手動予想）があればそれ、無ければ朝の予想
  - threads_text        : スレッズの下書き（予想タブと同じ引数で threads_poster.build_post_text を呼ぶ）
  - posted_status       : そのレースの note記事・スレッズ投稿・朝9:00の自動投稿の状態
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

JST = ZoneInfo("Asia/Tokyo")

# 保存した人（collector_name）→ 画面に出す予想の名前
COLLECTOR_LABELS = {"auto_post": "展示後の自動予想", "auto_random": "展示後の自動固定"}
MANUAL_LABEL = "展示後の手動予想"
MORNING_LABEL = "朝の予想（展示前）"
STYLE_LABELS = {"normal": "通常の予想", "aim": "中穴〜万舟狙い"}


def _restore(snapshot):
    from result_tracker import restore_snapshot_frames

    return restore_snapshot_frames(snapshot)


def frames_from_snapshot(snapshot):
    """保存された予想 → (final, tickets, work)。読めなければ (None, None, None)。
    work は保存したときのレースの表（race_features。無ければ None）。"""
    final_df, tickets_df, _ = _restore(snapshot)
    if final_df is None or tickets_df is None or not len(final_df) or not len(tickets_df):
        return None, None, None
    if "stake" in tickets_df.columns:
        tickets_df["stake"] = pd.to_numeric(tickets_df["stake"], errors="coerce").fillna(0).astype(int)
    work = None
    try:
        rows = (json.loads((snapshot or {}).get("payload_json") or "{}") or {}).get("race_features")
        if isinstance(rows, list) and rows:
            work = pd.DataFrame(rows)
    except (TypeError, ValueError):
        work = None
    return final_df, tickets_df, work


def morning_frames(row):
    """朝の予想の行 → (final, tickets, None)。1着確率（p_first の配列）と朝の買い目だけ。"""
    pf = list((row or {}).get("p_first") or [])
    if len(pf) != 6:
        return None, None, None
    names = {}
    if row.get("favorite_lane") is not None and row.get("favorite_name"):
        names[int(row["favorite_lane"])] = str(row["favorite_name"])
    final = pd.DataFrame({"lane": list(range(1, 7)), "racer_name": [names.get(ln, "") for ln in range(1, 7)],
                          "p_first": [float(x or 0) for x in pf]})
    tickets = pd.DataFrame([{"combo": str(t.get("combo")), "group": t.get("group") or "抑え",
                             "prob": float(t.get("prob") or 0), "stake": int(float(t.get("stake") or 0))}
                            for t in (row.get("tickets") or []) if t.get("combo")])
    if tickets.empty:
        return None, None, None
    return final, tickets, None


def is_post_exhibition(snapshot):
    """展示後の予想か（展示前に予想して保存したものは朝の予想と同じ扱いにする）。"""
    return bool(snapshot) and snapshot.get("pre_exhibition") is not True and bool(snapshot.get("payload_json"))


def prediction_label(snapshot):
    who = str((snapshot or {}).get("collector_name") or "")
    return COLLECTOR_LABELS.get(who, MANUAL_LABEL)


def choose_prediction(snapshot, row):
    """一覧で使う予想: {"final", "tickets", "work", "kind": "post"/"morning", "label", "saved_at"}。
    どちらも使えなければ None。"""
    if is_post_exhibition(snapshot):
        final, tickets, work = frames_from_snapshot(snapshot)
        if final is not None:
            return {"final": final, "tickets": tickets, "work": work, "kind": "post",
                    "label": prediction_label(snapshot), "saved_at": snapshot.get("saved_at")}
    final, tickets, work = morning_frames(row)
    if final is None:
        return None
    return {"final": final, "tickets": tickets, "work": work, "kind": "morning", "label": MORNING_LABEL,
            "saved_at": None}


def style_for(ticket_source):
    """note に載せる買い目 → スレッズの文章（中穴〜万舟狙いなら aim、それ以外は通常の予想）。"""
    return "aim" if ticket_source == "aim" else "normal"


def threads_text(d, venue, rno, final, tickets, deadline, pub=None, style="normal"):
    """スレッズの下書き。予想タブと一覧で同じ文章にする。"""
    import threads_poster as tp

    return tp.build_post_text(
        race_date=d.strftime("%-m/%-d") if hasattr(d, "strftime") else str(d),
        venue=venue,
        race_no=rno,
        final=final,
        tickets=tickets,
        deadline=deadline,
        # note は全て有料。記事にしていないレースも有料記事の案内の型にする
        publication_type=str((pub or {}).get("publication_type") or "PAID"),
        style=style,
    )


def jst_hhmm(ts):
    """データベースの時刻（世界標準時）を日本時間の HH:MM に。読めなければ空。"""
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=JST)
    return t.astimezone(JST).strftime("%H:%M")


def morning_auto_post(auto_row, race_key):
    """朝9:00の自動投稿（threads_auto_posts の1日1行）でこのレースを投稿したか。
    投稿していれば {"posted_at", "rank"}、予定に入っているだけなら {"posted_at": None, ...}、無ければ None。"""
    if not auto_row:
        return None
    for i, p in enumerate(auto_row.get("posts") or [], start=1):
        if (p or {}).get("race_key") == race_key:
            return {"posted": bool(p.get("post_id")), "posted_at": auto_row.get("posted_at") if p.get("post_id")
                    else None, "rank": i, "status": auto_row.get("status")}
    if race_key in (auto_row.get("race_keys") or []):
        return {"posted": False, "posted_at": None, "rank": None, "status": auto_row.get("status")}
    return None


def posted_status(race_key, notes=(), threads=(), auto_row=None):
    """そのレースの note記事・スレッズ投稿・朝の自動投稿の状態。
    notes: note_publications の行、threads: threads_race_posts の行（どちらもその日の分）。"""
    note = next((n for n in notes if n.get("race_key") == race_key), None)
    posts = sorted((t for t in threads if t.get("race_key") == race_key), key=lambda t: str(t.get("posted_at") or ""))
    return {"note": note, "threads": posts, "auto": morning_auto_post(auto_row, race_key)}


def status_lines(s):
    """一覧のレースに出す短い文のリスト（例: 📝 note 確定済み（10:12）・🧵 投稿済み（10:20））。"""
    out = []
    if s.get("note"):
        out.append(f"📝 note記事: 確定済み（{jst_hhmm(s['note'].get('created_at')) or '時刻不明'}）")
    if s.get("threads"):
        times = "・".join(jst_hhmm(t.get("posted_at")) or "時刻不明" for t in s["threads"])
        out.append(f"🧵 スレッズ: 投稿済み（{times}）")
    auto = s.get("auto")
    if auto:
        if auto["posted"]:
            out.append(f"🧵 毎朝9:00の自動投稿（厳選上位3レース）で投稿済み（{jst_hhmm(auto['posted_at']) or '9:00'}）")
        else:
            out.append("🧵 毎朝9:00の自動投稿（厳選上位3レース）に選ばれたレースです（投稿は失敗・未完了）")
    return out


def needs_confirm(s):
    """スレッズにもう一度投稿する前に確認がいるか（このアプリで投稿済み・朝の自動投稿で投稿済み）。"""
    return bool(s.get("threads")) or bool((s.get("auto") or {}).get("posted"))


def threads_record(race_key, post_id, text, style, ticket_source, prediction, screen):
    """threads_race_posts に書く行。"""
    k = str(race_key)
    return {"race_key": k, "race_date": f"{k[:4]}-{k[4:6]}-{k[6:8]}", "post_id": str(post_id or "") or None,
            "text": text, "style": style, "ticket_source": ticket_source, "prediction": prediction,
            "screen": screen}
