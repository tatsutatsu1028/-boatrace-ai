from __future__ import annotations

import io
import re

import pandas as pd

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False


_NUM_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")

# 「一周」タイムは他の展示データ（チルト・展示・まわり足・直線）と比べて
# 明確に大きい値（だいたい30〜45秒程度）になるのが通例。位置（列の順番）
# だけに頼ると、OCRが1マス読み落とした場合に列がずれて誤爆するため、
# 一周だけは「値の大きさ」でも判定できるようにする。
_LAP_MIN = 15.0


def extract_original_exhibition(image_bytes, n_lanes=6):
    """
    「オリジナル展示」表のスクリーンショット(bytes)から、
    一周・まわり足・直線の3列を読み取り、
    lane(1〜n_lanes) / original_straight / original_turn / original_lap
    のDataFrameを返す。読み取れない場合はNoneを返す。

    表の正確なレイアウト（列位置・列数）は公開している場ごとに異なる
    可能性があるため、決め打ちの座標には頼らず、
      1. 数値らしいトークンをすべてOCRで拾う
      2. Y座標が近いものを1行（1艇分）としてグルーピングする
      3. 各行の中で「一周」は値の大きさ（15以上）で特定する
      4. 残りのうちX座標が右側の2つを「まわり足・直線」の順とみなす
        （多くの場のオリジナル展示表がこの並び）
    という緩い方式で抽出する。あくまで下書き用の仮入力であり、
    実際の値は呼び出し側のテーブルで必ず確認・修正してもらう前提。
    """
    if not OCR_AVAILABLE:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:
        return None

    try:
        data = pytesseract.image_to_data(
            img,
            config="--psm 11 -c tessedit_char_whitelist=0123456789.",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return None

    tokens = []
    n = len(data.get("text", []))
    for i in range(n):
        t = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        if not t or conf < 40 or not _NUM_RE.match(t):
            continue
        cx = data["left"][i] + data["width"][i] / 2.0
        cy = data["top"][i] + data["height"][i] / 2.0
        tokens.append({"text": t, "value": float(t), "x": cx, "y": cy})

    if len(tokens) < n_lanes * 3:
        return None

    tokens.sort(key=lambda r: r["y"])

    # Y座標が近いものをまとめて1行（1艇分）にする。
    rows = []
    current = []
    last_y = None
    for tok in tokens:
        if last_y is not None and tok["y"] - last_y > 25:
            if current:
                rows.append(current)
            current = []
        current.append(tok)
        last_y = tok["y"]
    if current:
        rows.append(current)

    candidate_rows = [r for r in rows if len(r) >= 3]
    if len(candidate_rows) < n_lanes:
        return None

    out_rows = []
    for lane, row in enumerate(candidate_rows[:n_lanes], start=1):
        lap_candidates = [t for t in row if t["value"] >= _LAP_MIN]

        lap_val = None
        rest = row
        if lap_candidates:
            # 一周らしき値が複数あれば、表の右寄り（一周は展示より右にある）
            # を優先しつつ、最初に見つかったものを採用する。
            lap_tok = lap_candidates[0]
            lap_val = lap_tok["value"]
            rest = [t for t in row if t is not lap_tok]

        rest_sorted = sorted(rest, key=lambda r: r["x"])
        last2 = rest_sorted[-2:] if len(rest_sorted) >= 2 else []

        turn_val = last2[0]["value"] if len(last2) >= 1 else None
        straight_val = last2[1]["value"] if len(last2) >= 2 else None

        out_rows.append({
            "lane": lane,
            "original_lap": lap_val,
            "original_turn": turn_val,
            "original_straight": straight_val,
        })

    return pd.DataFrame(out_rows)
