"""
note の有料記事（展示後）の本文と、記事に載せる買い目。

  - 載せる買い目は「モデルのみ予想」（初期値）か「両方の予想平均」か「中穴〜万舟狙いの買い目」。両方の予想平均は、
    そのレースで Claude の予想を取ったとき（保存済み予想の "claude" に avg_tickets があるとき）だけ、
    中穴〜万舟狙いの買い目は万舟チャンスのレースで買い目を作ってあるとき（manshu_chance_picks.aim_tickets）だけ選べる。
    中穴〜万舟狙いの買い目は本線・抑えではなく「中穴」「万舟」に分けて載せ、本文も荒れる展開を中心に書く
  - 本文は Claude（Haiku）が、選んだ買い目と展示後の予想展開をフレンドリーに書く
    （write_with_claude）。データに無いことは書かない・断定しすぎない（「絶対」「鉄板」は使わない）
  - 買い目の欄は Claude に書かせず、選んだ買い目からこのファイルで作って本文の後ろに付ける
    （記事・予想カード・夜のまとめの的中判定がずれないように）
  - Claude が失敗した・守るべきことを破ったときは、今までの決まった型（template_article）にする
  - note に貼るときは本文を「無料部分」と「有料部分」に分ける（split_article）。無料部分はレース名・締切・
    見どころ・展開の入りだけで、買い目（組番）は入れない。noteの有料ラインは段落の境目にしか置けないため

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
ARTICLE_PROMPT_VERSION = "note-article-v3"
SOURCES = ("model", "avg", "aim")
SOURCE_LABELS = {"model": "モデルのみ予想", "avg": "両方の予想平均", "aim": "中穴〜万舟狙いの買い目"}
AIM_GROUPS = {"中穴": "中穴（オッズ30〜99倍）", "万舟": "万舟（オッズ100倍以上）"}
AIM_NOTE = ("※中穴〜万舟狙いの買い目は、1号艇の1着を外し、3連単オッズ30倍以上の組からモデルの確率が高い順に"
            "選んだ高配当狙いの買い目です。当たる確率は低めです。オッズは締切まで動きます。")
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
  組番を書いてよいのは ticket_comment だけ（lead・development は無料で読める部分に載るため、組番は書かない）。
- development の最初の1文は、組番や本命を明かさない「展開の入り」（スタートの見立てなど）にする。
- 買い目の一覧そのものは書かない（記事の後ろにこちらで付ける）。

予想の種類が「中穴〜万舟狙いの買い目」のとき:
- 載せる買い目は本線・抑えではなく、中穴（オッズ30〜99倍）と万舟（オッズ100倍以上）の組。どれも1号艇の1着を外している。
- development は、1号艇以外が1着になる荒れる展開（スタートの遅れ・まくり・差しなど、データから言えること）を中心に書く。
- ticket_comment では、中穴と万舟をどう狙うか（荒れる筋の考え方）を書く。「本線」「抑え」という言葉は使わない。
- 当たりやすいとは書かない（高配当狙いで当たる確率は低めであることに触れてよい）。"""

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


def aim_option(mark):
    """「中穴〜万舟狙いの買い目」 [{combo, group: "中穴"/"万舟"}]。mark は万舟チャンスの行（manshu_chance_picks）。
    買い目を作っていなければ None。"""
    rows = (mark or {}).get("aim_tickets") or []
    out = [{"combo": str(t["combo"]).strip(), "group": t.get("tier") if t.get("tier") in AIM_GROUPS else "万舟"}
           for t in rows if str(t.get("combo") or "").strip()]
    return out or None


def is_aim(tickets):
    """中穴〜万舟狙いの買い目か（区分が中穴・万舟だけ）。"""
    t = tickets_list(tickets)
    return bool(t) and all(x["group"] in AIM_GROUPS for x in t)


def aim_groups(tickets):
    """中穴〜万舟狙いの買い目 → (中穴の組番, 万舟の組番)。"""
    t = tickets_list(tickets)
    return [x["combo"] for x in t if x["group"] == "中穴"], [x["combo"] for x in t if x["group"] == "万舟"]


def ticket_block(tickets):
    """記事の後ろに付ける買い目の欄（今までの記事と同じ型。中穴〜万舟狙いの買い目は中穴・万舟に分ける）。"""
    if is_aim(tickets):
        mid, high = aim_groups(tickets)
        lines = [f"【{AIM_GROUPS['中穴']}】"] + (compact_combos(mid) or ["なし"])
        lines += ["", f"【{AIM_GROUPS['万舟']}】"] + (compact_combos(high) or ["なし"])
        return "\n".join(lines + ["", AIM_NOTE, ""] + DISCLAIMER)
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
    brief.pop("model_version", None)
    brief["race"]["締切"] = deadline or None
    brief["1マークの想定の並び"] = "-".join(str(x) for x in order) if order else None
    if is_aim(tickets):
        mid, high = aim_groups(tickets)
        brief["載せる買い目"] = {AIM_GROUPS["中穴"]: mid, AIM_GROUPS["万舟"]: high,
                             "予想の種類": SOURCE_LABELS["aim"]}
    else:
        main, cover = split_main_cover(tickets)
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


_COMBO_RE = re.compile(r"(?<![\d-])[1-6]-[1-6]-[1-6](?![\d-])|[1-6]-[1-6]{2,}|[1-6]=[1-6]")
_PAID_HEADS = ("■ 買い目の考え方", "【本線買い目】", "【抑え買い目】", "【中穴", "【万舟", "※")
_DEV_HEAD = "■ 展示後の予想展開"
PAYWALL_LEAD = "ここから先は有料部分です。展示後の詳しい予想展開と、買い目（本線・抑え）をお届けします。"
PAYWALL_LEAD_AIM = "ここから先は有料部分です。荒れる展開の読みと、中穴〜万舟狙いの買い目をお届けします。"


def _has_combo(text):
    return bool(_COMBO_RE.search(text))


def _first_sentence(text):
    """最初の1文と残り。1文しかないときは ("", text)（入りだけ出すと展開を全部見せてしまうため）。"""
    m = re.search(r"[。！？!?]", text)
    if not m or not text[m.end():].strip():
        return "", text
    return text[:m.end()].strip(), text[m.end():].strip()


def split_article(body, race_label="", deadline=""):
    """
    本文を note に貼るための (無料部分, 有料部分) に分ける。どちらも段落を空行1つで区切った文字列。
      無料: 【レース名｜締切】・あいさつ/見どころ・展開の入り（予想展開の最初の1文）・有料部分への案内
      有料: 展示後の予想展開の続き・買い目の考え方・本線/抑えの買い目・注意書き
    組番が入った段落は、見出しに関わらず有料側に回す（無料で買い目が見えないように）。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(body or "").strip()) if p.strip()]
    head = f"【{race_label}｜締切 {deadline}】" if race_label and deadline else (f"【{race_label}】" if race_label else "")
    free, paid = ([head] if head else []), []
    in_paid = False
    for p in paras:
        if in_paid or p.startswith(_PAID_HEADS):
            in_paid = True
            paid.append(p)
        elif p.startswith(_DEV_HEAD):
            in_paid = True
            dev = p[len(_DEV_HEAD):].strip()
            intro, rest = _first_sentence(dev)
            if intro and not _has_combo(intro):
                free.append(f"{_DEV_HEAD}\n{intro}")
                paid.append(f"{_DEV_HEAD}（続き）\n{rest}")
            else:
                paid.append(p)
        elif _has_combo(p):
            paid.append(p)
        else:
            free.append(p)
    if len(free) <= (1 if head else 0):
        free.append("展示航走を見たうえでの予想です。")
    free.append(PAYWALL_LEAD_AIM if any(p.startswith(("【中穴", "【万舟")) for p in paid) else PAYWALL_LEAD)
    return "\n\n".join(free), "\n\n".join(paid)


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
