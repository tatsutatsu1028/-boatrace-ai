"""
万舟チャンスのメモ入り予想を、オーナー専用の Discord チャンネルに通知する（manshu_source.py から呼ぶ）。

  - 通知先は環境変数 DISCORD_MEMO_WEBHOOK_URL（GitHub Secrets。今の締切10分前の通知とは別のウェブフック）。
    未設定なら何もしない
  - claude_memo・blend_memo を作ったとき（auto_post_predict.yml の自動記録）に送る。メモを変えて作り直したときは
    「更新」と分かる形でもう一度送る。メモの無いレース・締切後は送らない
  - 中身: 会場・R・締切、艇ごとのメモ、claude_memo と blend_memo の買い目（区分とオッズ付き）、参考にモデルの買い目
  - メモを外に出すのはこのチャンネルだけ。締切10分前の通知（pg_cron）・スレッズ・note・スタッフの画面には出さない
  - Actions のログは誰でも見られるため、戻り値（ログに出す文）には会場名・メモ・買い目を入れない
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ENV = "DISCORD_MEMO_WEBHOOK_URL"
LIMIT = 2000   # Discord の1通の文字数の上限
MEMO_LIMIT = 1000   # メモの欄の上限（保存できるメモと同じ長さ。長すぎても買い目が切れないように）
TIERS = ("中穴", "万舟")


def webhook_url():
    return os.environ.get(ENV, "").strip()


def _hhmm(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(JST).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def ticket_lines(tickets):
    """買い目を区分ごとに1行（例: 中穴: 2-1-3 45.0倍 / 3-1-2 60.2倍）。"""
    import manshu

    by = {}
    for x in tickets or []:
        tier = x.get("tier") or manshu.odds_tier(x.get("odds")) or "—"
        by.setdefault(tier, []).append(f"{x['combo']} {float(x['odds']):.1f}倍")
    order = list(TIERS) + [k for k in by if k not in TIERS]
    return [f"{k}: " + " / ".join(by[k]) for k in order if by.get(k)]


def _section(title, row):
    """1つの予想の欄。作れなかったときは理由。"""
    tickets = (row or {}).get("aim_tickets") or []
    if tickets:
        return [f"**{title}**（{len(tickets)}点）"] + ticket_lines(tickets)
    return [f"**{title}**", f"作れませんでした（{(row or {}).get('error') or '買い目なし'}）"]


def build_message(t, memo, rows, model_tickets, odds_at=None, update=False):
    """通知の文。t: 対象レース（jcd・venue・race_no・deadline）/ memo: 保存されたメモの文 /
    rows: {source: manshu_source_compare の行}（claude_memo・blend_memo）/ model_tickets: 参考のモデルの買い目。"""
    from official_fetcher import VENUES

    import manshu_source as ms

    venue = VENUES.get(str(t.get("jcd")).zfill(2), t.get("venue") or "")
    head = "📝 メモ入り予想【更新】" if update else "📝 メモ入り予想"
    lines = [f"{head}　**{venue} {t['race_no']}R**　締切 {t.get('deadline') or '--:--'}"]
    if odds_at and _hhmm(odds_at):
        lines.append(f"（オッズ {_hhmm(odds_at)} 時点）")
    memo = "\n".join(x.strip() for x in str(memo or "").splitlines() if x.strip())
    lines += ["", "**メモ**", memo if len(memo) <= MEMO_LIMIT else memo[:MEMO_LIMIT - 1] + "…"]
    lines += [""] + _section("メモ入りのClaude（claude_memo）", rows.get(ms.MEMO_SOURCE))
    lines += [""] + _section("メモ入りの平均（blend_memo）", rows.get(ms.MEMO_BLEND))
    lines += [""] + _section("参考: モデルの買い目", {"aim_tickets": model_tickets, "error": "オッズ30倍以上の組なし"})
    text = "\n".join(lines)
    return text if len(text) <= LIMIT else text[:LIMIT - 1] + "…"


def before_deadline(t, now=None):
    import manshu_source as ms

    at = ms.deadline_at(t.get("race_date"), t.get("deadline"))
    if not at:
        return False
    return (now or datetime.now(JST)) < datetime.fromisoformat(at)


def send(text, url=None, timeout=10):
    import requests

    r = requests.post(url or webhook_url(), json={"content": text, "allowed_mentions": {"parse": []}}, timeout=timeout)
    r.raise_for_status()


def notify(t, memo, rows, model_tickets, odds_at=None, update=False, now=None, sender=None):
    """メモ入り予想を通知する。戻り値はログ用の短い文（送らなかったときは空。会場名・メモ・買い目は入れない）。"""
    url = webhook_url()
    if not url or not str(memo or "").strip():
        return ""
    if not before_deadline(t, now):
        return "・通知: 締切後のため送らず"
    try:
        (sender or send)(build_message(t, memo, rows, model_tickets, odds_at, update), url)
    except Exception as e:  # noqa: BLE001  通知に失敗しても記録は残す
        return f"・通知: 失敗（{type(e).__name__}）"
    return "・通知: 更新を送信" if update else "・通知: 送信"
