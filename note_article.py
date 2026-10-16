"""
note の有料記事（展示後）の本文と、記事に載せる買い目。

  - 載せる買い目は「モデルのみ予想」（初期値）か「両方の予想平均」。両方の予想平均は、
    そのレースで Claude の予想を取ったとき（保存済み予想の "claude" に avg_tickets があるとき）だけ選べる
  - 本文は Claude（Haiku）が、選んだ買い目と展示後の予想展開をフレンドリーに書く
    （write_with_claude）。データに無いことは書かない・断定しすぎない（「絶対」「鉄板」は使わない）
  - 買い目の欄は Claude に書かせず、選んだ買い目からこのファイルで作って本文の後ろに付ける
    （記事・予想カード・夜のまとめの的中判定がずれないように）
  - Claude が失敗した・守るべきことを破ったときは、今までの決まった型（template_article）にする

Streamlit には依存しない（tests で確かめられるように）。
"""

from __future__ import annotations

import json
import math
import re
import time

import pandas as pd

import claude_reader as cr
from threads_poster import compact_combos, split_main_cover

ARTICLE_MODEL = "claude-haiku-5-5"
ARTICLE_TIMEOUT_SEC = 45.0
ARTICLE_MAX_TOKENS = 6000
ARTICLE_PROMPT_VERSION = "note-article-v1"
SOURCES = ("model", "avg")
SOURCE_LABELS = {"model": "モデルのみ予想", "avg": "両方の予想平均"}
PRICE_YEN = 300
HASHTAGS = "#ボートレース #競艇 #展示 #予想"
BANNED_WORDS = ("絶対", "鉄板", "確実", "間違いな", "100%", "１００％", "必ず当た", "勝ち確")

DISCLAIMER = [
    "※資金配分は指定していません。",
    "オッズとご自身の予算に合わせて、購入する買い目・金額をご判断ください。",
    "",
    "※掲載している予想は、的中や利益を保証するものではありません。舟券の購入はご自身の判断でお願いします。",
]

SYSTEM_PROMPT = """あなたはボートレース予想の note 記事を書くライターです。展示航走のあとに出す有料記事の本文を書きます。
渡されるのは、1レース分のデータ（出走表・展示・気象）、各艇の1着確率、想定ST・1マークの想定の並び、
記事に載せる3連単の買い目（本線・抑え）です。

書くもの:
- lead: 読者へのあいさつと、このレースの見どころ（60〜100字）
- development: 展示後の予想展開。スタート → 1マーク → 2着・3着争いの順に（200〜350字）
- ticket_comment: 載せる買い目の組み立ての考え方（本線と抑えをどう分けたか。80〜150字）

守ること:
- フレンドリーで読みやすい口調（です・ます）。絵文字は使ってもよいが多くしない（全体で3つまで）。
- 渡されたデータに書かれていることだけを使う。データに無いこと（選手の評判・過去の対戦・記者コメント・
  ピットの様子・オッズ・モーターの整備内容など）は書かない。値が空欄の項目には触れない。
- 数値を挙げるときは入力の値をそのまま使う。
- 断定しすぎない（「〜と見ます」「〜が有力」「〜に期待」など）。「絶対」「鉄板」「確実」「間違いない」は使わない。
- 3連単の組番（1-2-3 の形）を書くときは、渡された買い目に入っている組番だけにする。
- 買い目の一覧そのものは書かない（記事の後ろにこちらで付ける）。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {"type": "string"},
        "development": {"type": "string"},
        "ticket_comment": {"type": "string"},
    },
    "required": ["lead", "development", "ticket_comment"],
    "additionalProperties": False,
}

_CACHE = {}


# ---------------------------------------------------------------
# 載せる買い目
# ---------------------------------------------------------------
def tickets_list(tickets):
    """買い目（DataFrame か dict のリスト）→ [{combo, group}]（並び順はそのまま）。"""
    if tickets is None:
        return []
    rows = tickets.to_dict("records") if hasattr(tickets, "to_dict") else list(tickets)
    out = []
    for t in rows:
        combo = str(t.get("combo") or "").strip()
        if combo:
            out.append({"combo": combo, "group": str(t.get("group") or "抑え").strip() or "抑え"})
    return out


def avg_option(section, session_bundle=None):
    """「両方の予想平均」の買い目と1着確率 (tickets, {lane: p}) 。Claude の予想を取っていなければ None。

    section は保存済み予想の "claude"、session_bundle は claude_tab のこのセッションの予想。
    """
    if session_bundle is not None:
        t = (session_bundle.get("tickets") or {}).get("avg")
        af = session_bundle.get("avg_final")
        if t is not None and len(t):
            probs = ({int(r.lane): float(r.p_first) for r in af.itertuples()} if af is not None else {})
            return tickets_list(t), probs
    if isinstance(section, dict) and section.get("status") == "ok" and section.get("avg_tickets"):
        probs = {int(k): float(v) for k, v in (section.get("avg_p_first") or {}).items()}
        return tickets_list(section["avg_tickets"]), probs
    return None


def ticket_block(tickets):
    """記事の後ろに付ける買い目の欄（今までの記事と同じ型）。"""
    main, cover = split_main_cover(tickets)
    lines = ["【本線買い目】"] + (compact_combos(main) or ["なし"])
    lines += ["", "【抑え買い目】"] + (compact_combos(cover) or ["なし"])
    return "\n".join(lines + [""] + DISCLAIMER)


def template_article(tickets):
    """Claude が使えないときの決まった型（今までの note 記事の本文）。"""
    return ticket_block(tickets)


def article_title(venue, race_no, deadline=""):
    return f"【有料予想】{venue} {race_no}R｜締切 {deadline}" if deadline else f"【有料予想】{venue} {race_no}R"


# ---------------------------------------------------------------
# Claude の本文
# ---------------------------------------------------------------
def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def build_brief(work, final, tickets, p_first, visual_rows, race_label, deadline, source):
    """Claude に渡すデータ（JSON にできる dict）。1着確率は載せる買い目と同じ予想のものを使う。"""
    brief = cr.build_brief(work, final, race_label) if work is not None and len(work) else {"race": {}, "boats": []}
    for b in brief.get("boats", []):
        for k in ("モデル1着確率", "モデル2着確率", "モデル3着確率"):
            b.pop(k, None)
        p = p_first.get(int(b["艇番"]))
        if p is not None:
            b["1着確率"] = round(float(p), 3)
    vis = {int(r["lane"]): r for r in (visual_rows or [])}
    for b in brief.get("boats", []):
        r = vis.get(int(b["艇番"]))
        if r is not None:
            b["想定ST"] = round(float(r["st"]), 2)
    order = [r["lane"] for r in sorted(visual_rows or [], key=lambda x: x["turn_rank"])]
    main, cover = split_main_cover(tickets)
    brief.pop("model_version", None)
    brief["race"]["締切"] = deadline or None
    brief["1マークの想定の並び"] = "-".join(str(x) for x in order) if order else None
    brief["載せる買い目"] = {"本線": main, "抑え": cover, "予想の種類": SOURCE_LABELS.get(source, source)}
    return brief


def check_text(text, tickets):
    """守るべきことを破っていないか。破っていれば理由、問題なければ None。"""
    for w in BANNED_WORDS:
        if w in text:
            return f"使わない言葉（{w}）"
    allowed = {t["combo"] for t in tickets_list(tickets)}
    for combo in re.findall(r"(?<![\d-])([1-6]-[1-6]-[1-6])(?![\d-])", text):
        if combo not in allowed:
            return f"載せていない組番（{combo}）"
    return None


def compose(parts, tickets):
    """Claude の文章（lead・development・ticket_comment）と買い目の欄をつなげて本文にする。"""
    return "\n\n".join([
        parts["lead"].strip(),
        "■ 展示後の予想展開\n" + parts["development"].strip(),
        "■ 買い目の考え方\n" + parts["ticket_comment"].strip(),
        ticket_block(tickets),
    ])


def write_with_claude(api_key, race_key, brief, tickets, model=ARTICLE_MODEL, force=False):
    """
    Claude（Haiku）で本文を書く。例外は出さない。
      {"status": "ok"|"error", "body", "model", "served_model", "usage", "cost", "seconds", "error", "called_api"}
    同じレース・同じデータ・同じ買い目なら、このプロセスの中では1回だけ呼ぶ（force=True で書き直す）。
    """
    key = (str(race_key), model, json.dumps(brief, ensure_ascii=False, sort_keys=True), ARTICLE_PROMPT_VERSION)
    if key in _CACHE and not force:
        return _CACHE[key]
    out = {"status": "error", "model": model}
    if not api_key:
        out["error"] = "ANTHROPIC_API_KEY が未設定です"
        return out
    t0 = time.time()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=ARTICLE_TIMEOUT_SEC, max_retries=1)
        resp = client.messages.create(
            model=model,
            max_tokens=ARTICLE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
        )
        out["seconds"] = round(time.time() - t0, 1)
        out["called_api"] = True
        out["served_model"] = getattr(resp, "model", model)
        usage = getattr(resp, "usage", None)
        out["usage"] = {k: getattr(usage, k, None) or 0 for k in
                        ("input_tokens", "output_tokens", "cache_read_input_tokens",
                         "cache_creation_input_tokens")} if usage else {}
        out["cost"] = cr.estimate_cost(model, out["usage"])
        if resp.stop_reason == "refusal":
            out["error"] = "Claude が回答を控えました"
            return out
        if resp.stop_reason == "max_tokens":
            out["error"] = "回答が途中で切れました"
            return out
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        parts = json.loads(text)
        if not all(str(parts.get(k) or "").strip() for k in ("lead", "development", "ticket_comment")):
            out["error"] = "本文が空でした"
            return out
        problem = check_text(" ".join(str(parts[k]) for k in ("lead", "development", "ticket_comment")), tickets)
        if problem:
            out["error"] = f"守るべきことを破っていたため使いません: {problem}"
            return out
        out.update({"status": "ok", "body": compose(parts, tickets), "parts": parts})
        _CACHE[key] = out
        return out
    except Exception as e:  # noqa: BLE001  失敗しても記事は決まった型で作れる
        out["seconds"] = round(time.time() - t0, 1)
        out["error"] = type(e).__name__
        return out


def same_tickets(a, b):
    return [(t["combo"], t["group"]) for t in tickets_list(a)] == [(t["combo"], t["group"]) for t in tickets_list(b)]


def p_first_map(final):
    if final is None or not len(final):
        return {}
    lanes = pd.to_numeric(final["lane"], errors="coerce")
    probs = pd.to_numeric(final["p_first"], errors="coerce")
    return {int(ln): float(p) for ln, p in zip(lanes, probs) if ln == ln and p == p}
