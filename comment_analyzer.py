
import re

AXES = ["伸び", "出足", "回り足", "乗り心地", "総合気配"]

POS = {
    "伸び": ["伸びはいい","伸びがいい","伸びる","直線はいい","直線がいい","伸び寄り","行き足がいい"],
    "出足": ["出足はいい","出足がいい","出足は良い","出足が良い","押しがいい","立ち上がりがいい"],
    "回り足": ["回り足はいい","回り足がいい","ターン回りはいい","旋回はいい","回った後がいい"],
    "乗り心地": ["乗りやすい","乗り心地はいい","乗り心地がいい","操縦性はいい"],
    "総合気配": ["足はいい","全体的にいい","バランスがいい","上向いた","良くなった","手応えがある",
             "悪くない","普通以上","納得","十分"]
}
NEG = {
    "伸び": ["伸びが弱い","伸びは弱い","伸びがない","直線が弱い","伸びられる"],
    "出足": ["出足が弱い","出足は弱い","出足がない","押していない"],
    "回り足": ["回り足が弱い","回り足は弱い","ターン回りが弱い","回れない","流れる"],
    "乗り心地": ["乗りにくい","乗りづらい","乗り心地が悪い","操縦性が悪い"],
    "総合気配": ["足は弱い","全体的に弱い","良くない","悪い","厳しい","もうひとつ","合っていない",
             "しっくりこない","調整が必要"]
}
STRONG = ["かなり","だいぶ","すごく","抜けて","上位","トップ級","抜群"]
WEAKEN = ["普通","まずまず","悪くない"]

def analyze_comment(text):
    text = "" if text is None else str(text)
    scores = {k: 0.0 for k in AXES}
    hits = []
    mult = 1.35 if any(w in text for w in STRONG) else 1.0
    for axis in AXES:
        for phrase in POS[axis]:
            if phrase in text:
                s = 1.0 * mult
                if phrase == "悪くない":
                    s = 0.35
                scores[axis] += s
                hits.append(("＋", axis, phrase))
        for phrase in NEG[axis]:
            if phrase in text:
                scores[axis] -= 1.0 * mult
                hits.append(("－", axis, phrase))
    # broad fallback
    if not hits:
        if re.search(r"(いい|良い|上向|手応え)", text):
            scores["総合気配"] += 0.35
        if re.search(r"(悪い|弱い|厳しい|合っていない)", text):
            scores["総合気配"] -= 0.35
    for k in scores:
        scores[k] = max(-2.0, min(2.0, scores[k]))
    return scores, hits

def total_comment_score(text):
    s, _ = analyze_comment(text)
    weights = {"伸び":0.25,"出足":0.25,"回り足":0.25,"乗り心地":0.10,"総合気配":0.15}
    return sum(s[k]*weights[k] for k in weights)
