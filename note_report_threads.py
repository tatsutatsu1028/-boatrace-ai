"""
結果報告記事（1日の締め。note_report.py）を note に公開したあと、その日の結果を Threads に自動投稿する。

記事表示ページの結果報告ページ（?view=note&report=日付&key=…、note_page.py）で note の URL を登録すると呼ばれる。
  - ON/OFF は app_settings.note_report_threads_enabled（設定タブ。既定 OFF）
  - 二重投稿を防ぐのはデータベース（note_report_register が「投稿してよい」と返した回だけ投稿する。投稿済みの日は二度と投稿しない）
  - 文面: あいさつと締めの一言は Claude（Haiku）がフレンドリーに書き、結果の行（記事本数・的中数・的中したレースと配当）は
    結果報告記事と同じ集計からこのファイルで作る（数字を Claude に書かせない）。最後に note の URL。Threads の500文字に収める
  - Claude が使えないときは決まった一言にする
  - 結果（成功・失敗）は note_report_post_done が記録し、Discord に送る
Streamlit には依存しない（tests で確かめられるように）。
"""

from __future__ import annotations

import json
import time

import threads_poster as tp

MODEL = "claude-haiku-5-5"
TIMEOUT_SEC = 30.0
LIMIT = tp.TEXT_LIMIT  # 500
INTRO_MAX, CLOSING_MAX = 80, 60
BANNED = ("絶対", "鉄板", "確実", "間違いな", "必ず")
DEFAULT_INTRO = "今日も1日お付き合いありがとうございました🙏"
NOTE_LEAD = "結果報告はnoteにまとめました👇"

SYSTEM_PROMPT = """あなたはボートレース予想アカウントの Threads 投稿を書く担当です。その日の note 予想の結果報告の投稿に付ける、
あいさつ（intro）と締めの一言（closing）だけを書きます。結果の数字の行と note の URL はこちらで付けます。

- intro: 1日の締めのあいさつと、結果へのひとこと（40〜80字）。フレンドリーなです・ます調。絵文字は1〜2個
- closing: 明日に向けた締めの一言（20〜60字）。絵文字は1個まで
- 渡された結果（的中数・的中したレース）と矛盾することを書かない。的中が無い日は当たったように書かない
- レース名・組番・配当・的中率などの数字は書かない（結果の行と重ならないように）
- 「絶対」「鉄板」「確実」「必ず」は使わない。煽りすぎない"""

SCHEMA = {"type": "object", "properties": {"intro": {"type": "string"}, "closing": {"type": "string"}},
          "required": ["intro", "closing"], "additionalProperties": False}


def facts(day, records):
    """Claude に渡す結果の要約（数字は書かせないが、内容と矛盾しないように渡す）。"""
    settled = [r for r in records if r.get("settled")]
    hits = [r for r in settled if r.get("hit")]
    return {"日付": tp._short_date(day), "記事の本数": len(records), "結果確定": len(settled), "的中": len(hits),
            "結果待ち": len(records) - len(settled),
            "的中したレース": [tp._race_label(r.get("venue"), r.get("race_no")) for r in hits]}


def result_lines(day, records, payouts):
    """結果の行: 見出し・記事本数と的中数・レースごとの結果（的中は配当つき）。"""
    head = f"📊 {tp._short_date(day)}のnote予想の結果"
    if not records:
        return [head, "本日のnote記事はありませんでした。"]
    return [head, tp._section_headline(records)] + tp._section_lines(records, payouts or {}, True)


def compose(intro, lines, closing, note_url, limit=LIMIT):
    """あいさつ・結果の行・締め・note の URL をつなげる。500文字を超えるときは、外れ・結果待ちのレースの行を後ろから省いて
    「ほかNレース」にし、それでも長ければ締め → あいさつ → 的中の行の順に省く（見出し・URL は必ず残す）。"""
    head, keep = list(lines[:2]), list(lines[2:])
    tail = f"{NOTE_LEAD}\n{note_url}"
    dropped = 0

    def build(i, c):
        body = head + keep + ([f"ほか{dropped}レース"] if dropped else [])
        return "\n\n".join(p for p in (i, "\n".join(body), c, tail) if p)

    def shrink(prefixes):
        nonlocal dropped
        idx = next((k for k in range(len(keep) - 1, -1, -1) if keep[k].startswith(prefixes)), None)
        if idx is None:
            return False
        keep.pop(idx)
        dropped += 1
        return True

    text = build(intro, closing)
    while len(text) > limit and shrink(("❌", "⏳")):
        text = build(intro, closing)
    for i, c in ((intro, ""), ("", "")):
        if len(text) > limit:
            text = build(i, c)
    while len(text) > limit and shrink(("⭕",)):
        text = build("", "")
    return text


def _clean_part(v, max_len):
    v = " ".join(str(v or "").split())
    if not v or len(v) > max_len or any(w in v for w in BANNED) or any(ch.isdigit() for ch in v):
        return None
    return v


def write_words(api_key, fact, model=None):
    """Claude（Haiku）であいさつと締めの一言を書く。例外は出さない。
    {"status", "intro", "closing", "error", "called_api", "usage", "cost", "model", "served_model", "seconds"}"""
    import claude_reader as cr

    model = model or MODEL
    out = {"status": "error", "model": model}
    if not api_key:
        out["error"] = "ANTHROPIC_API_KEY が未設定です"
        return out
    t0 = time.time()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SEC, max_retries=1)
        resp = client.messages.create(
            model=model, max_tokens=1000, system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(fact, ensure_ascii=False)}])
        out.update(called_api=True, served_model=getattr(resp, "model", model), seconds=round(time.time() - t0, 1))
        usage = getattr(resp, "usage", None)
        out["usage"] = {k: getattr(usage, k, None) or 0 for k in
                        ("input_tokens", "output_tokens", "cache_read_input_tokens",
                         "cache_creation_input_tokens")} if usage else {}
        out["cost"] = cr.estimate_cost(model, out["usage"])
        if resp.stop_reason in ("refusal", "max_tokens"):
            out["error"] = f"Claude の回答を使えませんでした（{resp.stop_reason}）"
            return out
        data = json.loads(next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "{}"))
        intro, closing = _clean_part(data.get("intro"), INTRO_MAX), _clean_part(data.get("closing"), CLOSING_MAX)
        if not intro or not closing:
            out["error"] = "守るべきこと（長さ・数字・使わない言葉）を破っていたため使いません"
            return out
        out.update(status="ok", intro=intro, closing=closing)
    except Exception as e:  # noqa: BLE001
        out.update(error=type(e).__name__, seconds=round(time.time() - t0, 1))
    return out


def build_text(day, records, payouts, note_url, api_key="", model=None):
    """投稿する文面と、Claude の結果 (text, res)。"""
    settled = [r for r in records if r.get("settled")]
    hits = [r for r in settled if r.get("hit")]
    res = write_words(api_key, facts(day, records), model)
    if res.get("status") == "ok":
        intro, closing = res["intro"], res["closing"]
    else:
        intro, closing = DEFAULT_INTRO, tp._closing_by_rate(hits, settled)
    return compose(intro, result_lines(day, records, payouts), closing, note_url), res


def post(sb_url, service_key, text):
    """既存の Threads 投稿（threads_poster）で投稿し、投稿IDを返す。記録（threads_posts）は「本日のまとめ」と同じ kind = summary。"""
    cfg = tp.load_config(sb_url, service_key)
    if not cfg:
        raise RuntimeError("Threads の連携情報を読めませんでした（設定タブで登録してください）")
    post_id = tp.post_text(cfg["user_id"], cfg["access_token"], text)
    tp.record_post(sb_url, service_key, post_id, "summary", text)
    return post_id
