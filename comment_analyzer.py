
from __future__ import annotations

import re
from typing import Dict, List, Tuple


AXES = ["伸び", "出足", "回り足", "乗り心地", "総合気配"]

# phrase, score
POS = {
    "伸び": [
        ("伸びはかなりいい", 1.55), ("伸びがかなりいい", 1.55),
        ("伸びはいい", 1.05), ("伸びがいい", 1.05), ("伸びは良い", 1.05), ("伸びが良い", 1.05),
        ("伸びる", 0.85), ("直線はいい", 0.95), ("直線がいい", 0.95),
        ("直線は良い", 0.95), ("直線が良い", 0.95),
        ("伸び寄り", 0.70), ("行き足がいい", 0.80), ("行き足はいい", 0.80),
        ("スリット付近はいい", 0.80), ("下がらない", 0.45),
    ],
    "出足": [
        ("出足はかなりいい", 1.55), ("出足がかなりいい", 1.55),
        ("出足はいい", 1.05), ("出足がいい", 1.05), ("出足は良い", 1.05), ("出足が良い", 1.05),
        ("押しがいい", 0.90), ("押している", 0.75),
        ("立ち上がりがいい", 0.85), ("レース足がいい", 0.75),
    ],
    "回り足": [
        ("回り足はかなりいい", 1.55), ("回り足がかなりいい", 1.55),
        ("回り足はいい", 1.05), ("回り足がいい", 1.05),
        ("ターン回りはいい", 1.00), ("ターン回りがいい", 1.00),
        ("旋回はいい", 0.85), ("回った後がいい", 0.95), ("回ってからがいい", 0.95),
        ("舟の向きがいい", 0.75), ("かかりがいい", 0.80),
    ],
    "乗り心地": [
        ("乗りやすい", 1.00), ("乗り心地はいい", 1.00), ("乗り心地がいい", 1.00),
        ("乗り心地は良い", 1.00), ("操縦性はいい", 0.90), ("ターンしやすい", 0.85),
    ],
    "総合気配": [
        ("足はかなりいい", 1.45), ("全体的にかなりいい", 1.45),
        ("足はいい", 0.90), ("足がいい", 0.90), ("全体的にいい", 0.90),
        ("バランスがいい", 0.85), ("上向いた", 0.75), ("良くなった", 0.70),
        ("手応えがある", 0.75), ("納得している", 0.70), ("十分", 0.45),
        ("悪くない", 0.30), ("普通以上", 0.35), ("まずまず", 0.20),
    ],
}

NEG = {
    "伸び": [
        ("伸びがかなり弱い", -1.55), ("伸びはかなり弱い", -1.55),
        ("伸びが弱い", -1.05), ("伸びは弱い", -1.05), ("伸びがない", -1.15),
        ("直線が弱い", -1.00), ("直線は弱い", -1.00), ("伸びられる", -0.85),
        ("下がる", -0.85), ("スリットから劣勢", -1.00),
    ],
    "出足": [
        ("出足がかなり弱い", -1.55), ("出足はかなり弱い", -1.55),
        ("出足が弱い", -1.05), ("出足は弱い", -1.05), ("出足がない", -1.15),
        ("押していない", -0.90), ("押しが弱い", -0.90),
    ],
    "回り足": [
        ("回り足がかなり弱い", -1.55), ("回り足はかなり弱い", -1.55),
        ("回り足が弱い", -1.05), ("回り足は弱い", -1.05),
        ("ターン回りが弱い", -1.00), ("ターン回りは弱い", -1.00),
        ("回れない", -1.05), ("流れる", -0.90), ("かからない", -0.90),
        ("舟が向かない", -0.90),
    ],
    "乗り心地": [
        ("乗りにくい", -1.00), ("乗りづらい", -1.00), ("乗り心地が悪い", -1.10),
        ("操縦性が悪い", -1.00), ("ターンしづらい", -0.90),
    ],
    "総合気配": [
        ("足はかなり弱い", -1.45), ("全体的にかなり弱い", -1.45),
        ("足は弱い", -0.95), ("全体的に弱い", -0.95), ("良くない", -0.90),
        ("悪い", -0.85), ("厳しい", -0.95), ("もうひとつ", -0.55),
        ("合っていない", -0.80), ("しっくりこない", -0.70),
        ("調整が必要", -0.50), ("調整途上", -0.55), ("まだ合っていない", -0.90),
        ("物足りない", -0.65),
    ],
}

STRONG_POS = ["かなり", "だいぶ", "すごく", "抜けて", "上位", "トップ級", "抜群", "節一", "一番いい"]
STRONG_NEG = ["全然", "かなり悪い", "かなり弱い", "厳しい"]
UNCERTAIN = [
    "分からない", "わからない", "何とも言えない", "微妙", "調整中",
    "合わせ切れていない", "まだ調整", "ペラをやる", "調整する", "迷っている"
]

# 強い自己評価に多少価値を置くが、コメントだけで確率を壊さないよう上限を設ける
AXIS_WEIGHTS = {
    "伸び": 0.27,
    "出足": 0.27,
    "回り足": 0.26,
    "乗り心地": 0.08,
    "総合気配": 0.12,
}


def _normalize(text) -> str:
    s = "" if text is None else str(text)
    s = s.replace("　", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


# フレーズ辞書は「〜はいい」「〜が悪い」等の言い切り形で作られているため、
# 実際の選手コメントに多い「〜て形」「〜た形」もこの形に正規化してから
# マッチングする。置換後の文字列長は元の語幹＋いい/悪い等になるため、
# フレーズ辞書の文言を変えずにヒット率を上げられる。
_CONJUGATION_MAP = [
    (r"良かった", "いい"), (r"よかった", "いい"), (r"良くて", "いい"),
    (r"よくて", "いい"), (r"良く", "いい"), (r"いいですね", "いい"),
    (r"悪かった", "悪い"), (r"悪くて", "悪い"),
    (r"やすかった", "やすい"), (r"やすくて", "やすい"),
    (r"にくかった", "にくい"), (r"にくくて", "にくい"),
    (r"づらかった", "づらい"), (r"づらくて", "づらい"),
    (r"弱かった", "弱い"), (r"弱くて", "弱い"),
    (r"強かった", "強い"), (r"強くて", "強い"),
]


def _normalize_conjugation(text: str) -> str:
    for pat, repl in _CONJUGATION_MAP:
        text = re.sub(pat, repl, text)
    return text


def analyze_comment(text):
    """
    後方互換API:
    return scores, hits
    """
    detail = analyze_comment_detail(text)
    return detail["scores"], detail["hits"]


def analyze_comment_detail(text):
    text = _normalize(text)
    text = _normalize_conjugation(text)
    scores = {k: 0.0 for k in AXES}
    hits: List[Tuple[str, str, str, float]] = []

    # 長いフレーズから評価し、同じ意味の短い語の二重加算を軽減
    matched_spans = []

    def overlaps(start, end):
        return any(not (end <= a or start >= b) for a, b in matched_spans)

    candidates = []
    for axis in AXES:
        for phrase, score in POS[axis]:
            candidates.append((phrase, axis, score))
        for phrase, score in NEG[axis]:
            candidates.append((phrase, axis, score))

    candidates.sort(key=lambda x: len(x[0]), reverse=True)

    for phrase, axis, score in candidates:
        for m in re.finditer(re.escape(phrase), text):
            if overlaps(m.start(), m.end()):
                continue
            scores[axis] += score
            sign = "＋" if score > 0 else "－"
            hits.append((sign, axis, phrase, score))
            matched_spans.append((m.start(), m.end()))

    # 一般語のfallback
    if not hits:
        if re.search(r"(いい|良い|上向|手応え|納得)", text):
            scores["総合気配"] += 0.30
            hits.append(("＋", "総合気配", "一般的な好感表現", 0.30))
        if re.search(r"(悪い|弱い|厳しい|合っていない|物足りない)", text):
            scores["総合気配"] -= 0.35
            hits.append(("－", "総合気配", "一般的な不安表現", -0.35))

    # 不確実・調整途上コメントは総合評価を少し減らす
    uncertain_hits = [w for w in UNCERTAIN if w in text]
    if uncertain_hits:
        scores["総合気配"] -= min(0.60, 0.18 * len(uncertain_hits))

    # 極端な値を抑制
    for k in scores:
        scores[k] = max(-2.0, min(2.0, scores[k]))

    weighted = sum(scores[k] * AXIS_WEIGHTS[k] for k in AXIS_WEIGHTS)
    weighted = max(-1.55, min(1.55, weighted))

    positive_axes = [k for k, v in scores.items() if v >= 0.45]
    negative_axes = [k for k, v in scores.items() if v <= -0.45]

    if weighted >= 0.75:
        grade = "◎"
    elif weighted >= 0.30:
        grade = "○"
    elif weighted <= -0.75:
        grade = "×"
    elif weighted <= -0.30:
        grade = "△"
    else:
        grade = "－"

    if positive_axes:
        summary = grade + " " + "・".join(positive_axes[:3])
    elif negative_axes:
        summary = grade + " " + "・".join(negative_axes[:3])
    elif text:
        summary = grade + " 大きな強弱なし"
    else:
        summary = "未入力"

    # 根拠が多いほどconfidence高め。ただしコメント自体は主観情報なので1.0にはしない
    evidence = len(hits)
    confidence = min(0.90, 0.35 + 0.10 * evidence) if text else 0.0
    if uncertain_hits:
        confidence *= 0.78

    return {
        "text": text,
        "scores": scores,
        "hits": hits,
        "total": weighted,
        "grade": grade,
        "summary": summary,
        "confidence": round(float(confidence), 3),
        "positive_axes": positive_axes,
        "negative_axes": negative_axes,
        "uncertain_hits": uncertain_hits,
    }


def total_comment_score(text):
    return analyze_comment_detail(text)["total"]
