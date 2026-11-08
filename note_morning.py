"""
note の「朝の記事＋自動更新」（厳選10だけ）。設定タブの「note の形」が「朝の記事＋自動更新」（app_settings.note_mode = morning）
で、自動下書きが ON のときだけ動く。

  python note_morning.py run [--date YYYYMMDD]   毎朝 9:02（pg_cron → note_morning.yml）。厳選10の確定を待って作る

1. 朝の記事（run）
   - 厳選10（pre_exhibition_picks）のレースごとに、展示前の予想展開と買い目（本線・抑え）の記事を作り、note_publications に
     status = AUTO_DRAFT・article_mode = morning で保存する（note_morning_save）。価格は300円
   - 本文は Claude（Haiku。手動の記事と同じ note_article の書き方を、展示前向けに変えたもの）。失敗したら決まった型
   - 無料部分の冒頭で「展示後に買い目を更新します。更新は有料部分のリンク先でご確認ください」と伝え、有料部分の最後に
     「展示後の最新買い目はこちら（締切10分前ごろ自動更新）」と購入者向けページの URL を付ける
     （URL はデータベースが保存するときに入れる。レースごとの乱数で、マスターキーとは別。コード・ログに残らない）
   - 締切まで MIN_LEAD_MINUTES 分を切ったレースは作らない
   - できたら「🌅 朝の記事N本ができました」と朝の一覧ページの URL を厳選10の Discord に送る（note_morning_notify）
2. 展示後の更新（update_post・mark_none。auto_post_predict.py から）
   - 展示後の自動予想を保存したら、展示後の予想展開と買い目を Claude で書き、購入者向けページを切り替える
     （note_morning_post_update。夜のまとめの的中判定も展示後の買い目になる）
   - 展示タイムがそろわないなどで予想できなかったレースは「更新なし」（展示前の買い目で判定）
Streamlit には依存しない。
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import note_article as na

JST = ZoneInfo("Asia/Tokyo")
MODE = "morning"
WAIT_PICKS_MINUTES = 40
MIN_LEAD_MINUTES = 10
UPDATE_LEAD = "締切10分前ごろ"
USAGE_PURPOSE = "note_article_morning"
POST_USAGE_PURPOSE = "note_article_morning_post"
PROMPT_VERSION = "note-article-morning-v1"

NOTICE = "⏰ この記事は展示前（朝）の予想です。展示後に買い目を更新します。更新は有料部分のリンク先でご確認ください。"
PRE_DEV_HEAD = "■ 展示前の予想展開"
LINK_HEAD = f"■ 展示後の最新買い目はこちら（{UPDATE_LEAD}自動更新）"
BUYER_URL = "{{BUYER_URL}}"
PAYWALL = ("ここから先は有料部分です。展示前の予想展開と買い目（本線・抑え）、"
           "展示後の最新買い目のページへのリンクをお届けします。")

SYSTEM_PROMPT = (na.SYSTEM_PROMPT
                 .replace("展示航走のあとに出す有料記事の本文を書きます。",
                          "展示航走の前（朝）に出す有料記事の本文を書きます。展示のデータ（展示タイム・展示ST・"
                          "直前の気象など）はまだ無いので、それには触れません。")
                 .replace("- development: 展示後の予想展開。", "- development: 展示前の予想展開。"))


def log(*a):
    print("[NOTE_MORNING]", *a, flush=True)


def enabled(settings):
    s = settings or {}
    return bool(s.get("note_auto_enabled")) and s.get("note_mode") == MODE


def compose(parts, tickets):
    """朝の記事の本文（{{BUYER_URL}} はデータベースが保存するときに URL にする）。parts が空なら決まった型。"""
    parts = parts or {}
    paras = [NOTICE]
    if str(parts.get("lead") or "").strip():
        paras.append(parts["lead"].strip())
    if str(parts.get("development") or "").strip():
        paras.append(f"{PRE_DEV_HEAD}\n" + parts["development"].strip())
    if str(parts.get("ticket_comment") or "").strip():
        paras.append("■ 買い目の考え方\n" + parts["ticket_comment"].strip())
    paras += [na.ticket_block(tickets), f"{LINK_HEAD}\n{BUYER_URL}"]
    return "\n\n".join(paras)


def pre_text(parts, tickets):
    """購入者向けページの「展示前」の欄（予想展開・買い目の考え方・買い目）。"""
    parts = parts or {}
    paras = []
    if str(parts.get("development") or "").strip():
        paras.append(f"{PRE_DEV_HEAD}\n" + parts["development"].strip())
    if str(parts.get("ticket_comment") or "").strip():
        paras.append("■ 買い目の考え方\n" + parts["ticket_comment"].strip())
    return "\n\n".join(paras + [na.ticket_block(tickets)])


def post_text(parts, tickets):
    """購入者向けページの「展示後」の欄。"""
    parts = parts or {}
    paras = []
    if str(parts.get("development") or "").strip():
        paras.append("■ 展示後の予想展開\n" + parts["development"].strip())
    if str(parts.get("ticket_comment") or "").strip():
        paras.append("■ 買い目の考え方\n" + parts["ticket_comment"].strip())
    return "\n\n".join(paras + [na.ticket_block(tickets)])


def _log_usage(sb, res, race_key, purpose):
    import claude_reader as cr

    row = cr.usage_row(res, race_key, purpose)
    if row:
        try:
            sb("POST", "claude_usage", prefer="return=minimal", json=row)
        except Exception as e:  # noqa: BLE001
            log(f"claude_usage の記録に失敗（{type(e).__name__}）")


def _write(sb, race_key, brief, tickets, system, version, purpose, api_key, model):
    """Claude で lead・development・ticket_comment を書く。(parts, writer, error)。失敗したら parts = None。"""
    res = na.write_with_claude(api_key, race_key, brief, tickets, model=model or na.ARTICLE_MODEL, system=system,
                               prompt_version=version, compose_fn=lambda p: "")
    if res.get("called_api"):
        _log_usage(sb, res, race_key, purpose)
    if res.get("status") == "ok":
        return res.get("parts"), "claude", None
    return None, "template", res.get("error")


def _brief(work, final, tickets, probs, label, deadline):
    from race_visuals import build_visual_rows

    import note_auto

    final_used = note_auto._final_with(final, probs)
    visual = build_visual_rows(final_used, work, top_combo=note_auto._top_combo(tickets))
    return na.build_brief(work, final_used, tickets, probs, visual, label, deadline, "model")


# ---------------------------------------------------------------
# 1. 朝の記事
# ---------------------------------------------------------------
def make_article(sb, row, work=None, api_key="", model=None):
    """厳選10の1レースの朝の記事を作って保存する。戻り値 {"status": created/updated/exists/failed, "writer", "error"}。"""
    import note_auto
    import race_posts

    out = {"race_key": row.get("race_key"), "status": "failed", "writer": None, "error": None}
    try:
        final, tickets_df, _ = race_posts.morning_frames(row)
        if final is None:
            raise RuntimeError("朝の予想（1着確率・買い目）を読めませんでした")
        tickets, probs = na.tickets_list(tickets_df), na.p_first_map(final)
        venue, rno, deadline = row.get("venue") or "", int(row["race_no"]), str(row.get("deadline") or "")
        label = f"{venue} {rno}R"
        brief = _brief(work, final, tickets, probs, label, deadline)
        parts, writer, err = _write(sb, row["race_key"], brief, tickets, SYSTEM_PROMPT, PROMPT_VERSION,
                                    USAGE_PURPOSE, api_key, model)
        out.update(writer=writer, error=err)
        body = compose(parts, tickets)
        text = note_auto.with_paywall_mark(body, label, deadline, PAYWALL)
        p1 = max(probs, key=lambda k: probs[k]) if probs else None
        saved = sb("POST", "rpc/note_morning_save", json={
            "p_race_key": row["race_key"], "p_publication_date": str(row["race_date"]), "p_venue": venue,
            "p_race_no": rno, "p_title": na.article_title(venue, rno, deadline), "p_article_text": text,
            "p_pre_text": pre_text(parts, tickets), "p_tickets": tickets, "p_article_writer": writer,
            "p_p1_lane": p1, "p_p1_prob": float(probs[p1]) if p1 else None, "p_price": na.PRICE_YEN,
        }).json()
        out["status"] = saved
        if saved in ("created", "updated"):
            note_auto.record_header(sb, {"race_key": row["race_key"], "sources": ["pre"]}, work)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return out


def _race_card(row):
    """展示前の出走表（Claude に渡す）。取れなければ None（1着確率と買い目だけで書く）。"""
    try:
        from official_fetcher import fetch_official_race

        race = fetch_official_race(str(row["race_key"])[:8], str(row["jcd"]).zfill(2), int(row["race_no"]))
        if race is None or not len(race):
            return None
        work = race.copy()
        work["date"], work["venue"], work["race_no"] = str(row["race_date"]), row.get("venue"), int(row["race_no"])
        return work
    except Exception:  # noqa: BLE001
        return None


def targets(rows, now, lead=MIN_LEAD_MINUTES):
    """締切まで lead 分以上あるレース（締切順）。"""
    out = []
    for r in rows or []:
        try:
            dl = datetime.strptime(f"{r['race_date']} {r['deadline']}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except (KeyError, TypeError, ValueError):
            continue
        if dl - now >= timedelta(minutes=lead):
            out.append(r)
    return sorted(out, key=lambda r: (str(r.get("deadline")), r["race_key"]))


def _sb(method, path, **kwargs):
    from auto_post_predict import _sb as sb

    return sb(method, path, **kwargs)


def _wait_for_picks(iso, minutes=WAIT_PICKS_MINUTES):
    until = time.time() + minutes * 60
    while True:
        if _sb("GET", f"pre_exhibition_runs?race_date=eq.{iso}&select=race_date").json():
            return True
        if time.time() > until:
            return False
        log("厳選10の確定を待っています")
        time.sleep(60)


def run(date8=None):
    from auto_random_fix import _load_settings

    settings = _load_settings()
    if not enabled(settings):
        log("朝の記事＋自動更新は OFF です（設定タブ）。何もしません")
        return 0
    now = datetime.now(JST)
    date8 = date8 or now.strftime("%Y%m%d")
    iso = f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"
    if not _wait_for_picks(iso):
        log("厳選10が確定しないため作りませんでした")
        return 0
    rows = _sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select=*&order=deadline.asc").json() or []
    todo = targets(rows, datetime.now(JST))
    log(f"厳選10 {len(rows)}レース・締切まで{MIN_LEAD_MINUTES}分以上 {len(todo)}レース")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("CLAUDE_ARTICLE_MODEL", "").strip() or None
    made = 0
    for r in todo:
        res = make_article(_sb, r, _race_card(r), api_key=api_key, model=model)
        made += res["status"] in ("created", "updated")
        # ログは誰でも見られるので、会場・買い目は出さない（締切・合否だけ）
        log(f"締切 {r.get('deadline')}: {res['status']}・本文 {res['writer']}"
            + (f"（{res['error'][:80]}）" if res.get("error") else ""))
    if made:
        _sb("POST", "rpc/note_morning_notify", json={"p_count": made, "p_date": iso})
    log(f"朝の記事 {made}本")
    return made


# ---------------------------------------------------------------
# 2. 展示後の更新
# ---------------------------------------------------------------
def applies(settings, t):
    """このレースは朝の記事（展示後は購入者向けページを更新する）か。"""
    return enabled(settings) and "pre" in ((t or {}).get("sources") or [])


def update_post(sb, t, final, tickets, work=None, api_key="", model=None):
    """展示後の予想展開と買い目を書いて、購入者向けページを切り替える。戻り値 {"status", "writer", "seconds", "error"}。"""
    t0 = time.time()
    out = {"status": "failed", "writer": None, "error": None}
    try:
        rows_t, probs = na.tickets_list(tickets), na.p_first_map(final)
        if not rows_t:
            raise RuntimeError("買い目がありません")
        label = f"{t.get('venue') or ''} {int(t['race_no'])}R"
        brief = _brief(work, final, rows_t, probs, label, str(t.get("deadline") or ""))
        parts, writer, err = _write(sb, t["race_key"], brief, rows_t, None, None, POST_USAGE_PURPOSE, api_key, model)
        out.update(writer=writer, error=err)
        out["status"] = sb("POST", "rpc/note_morning_post_update", json={
            "p_race_key": t["race_key"], "p_status": "updated", "p_post_text": post_text(parts, rows_t),
            "p_post_tickets": rows_t}).json()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    out["seconds"] = round(time.time() - t0, 1)
    return out


def update_post_from_env(sb, t, final, tickets, work=None):
    return update_post(sb, t, final, tickets, work, api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
                       model=os.environ.get("CLAUDE_ARTICLE_MODEL", "").strip() or None)


def mark_none(sb, t):
    """展示後に更新できなかったレース（購入者向けページは「展示後の更新はありません」）。"""
    try:
        return sb("POST", "rpc/note_morning_post_update", json={
            "p_race_key": t["race_key"], "p_status": "none", "p_post_text": None, "p_post_tickets": None}).json()
    except Exception as e:  # noqa: BLE001
        return f"エラー（{type(e).__name__}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["run"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    run(a.date)


if __name__ == "__main__":
    main()
