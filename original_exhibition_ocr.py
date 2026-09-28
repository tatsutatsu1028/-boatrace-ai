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
_NUM_RE_ANY = re.compile(r"\d{1,2}\.\d{1,2}")

# 「一周」タイムは、全場共通でだいたい 30〜42秒程度に収まる。
# 体重(40〜75kg程度)ともろに重なる「15以上」判定だと体重を一周と誤認
# しやすいため、まずはこの狭いレンジで判定し、見つからない場合だけ
# 昔ながらの緩い閾値(15以上)にフォールバックする。
_LAP_MIN = 30.0
_LAP_MAX = 42.0
_LAP_FALLBACK_MIN = 15.0

# チルト・調整はだいたい -3.0 〜 4.5 程度の小さい値。
_TILT_MIN = -3.0
_TILT_MAX = 4.5

# 展示タイムはどの場でもだいたい 6.3〜7.4 秒に収まる、かなり狭いレンジ。
# これを目印に「展示タイム列」を残り候補から取り除き、
# それより右にある値を「まわり足」「直線」の順とみなす。
_DISPLAY_MIN = 6.3
_DISPLAY_MAX = 7.4

# 行同士のY方向のまとめ幅、および「同じ行」とみなすためのY許容差。
_ROW_GAP = 25
_ROW_Y_TOLERANCE = 20

_OCR_CONFIG = "--psm 11 -c tessedit_char_whitelist=0123456789."


def _ocr_pass(img):
    """1枚の画像に対してOCRを実行し、(マッチしたトークン一覧, 生テキスト一覧)を返す。"""
    try:
        data = pytesseract.image_to_data(
            img, config=_OCR_CONFIG, output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return [], []

    matched = []
    raw = []
    n = len(data.get("text", []))
    for i in range(n):
        t = (data["text"][i] or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        cx = data["left"][i] + data["width"][i] / 2.0
        cy = data["top"][i] + data["height"][i] / 2.0
        raw.append({"text": t, "x": cx, "y": cy, "conf": conf})
        if conf >= 40 and _NUM_RE.match(t):
            matched.append({"text": t, "value": float(t), "x": cx, "y": cy})
    return matched, raw


def _is_meaningful(row):
    """体重(>42)やチルト・調整(-3〜4.5)だけの行は、一周・まわり足・直線の
    どれにも該当しないので「1艇分の行」としてはノイズになる。
    そういう行が1行としてカウントされると、他の艇の行とズレて
    艇番の対応がずれてしまうため、あらかじめ除外する。"""
    for t in row:
        v = t["value"]
        if v <= _LAP_MAX and not (_TILT_MIN <= v <= _TILT_MAX):
            return True
    return False


def extract_original_exhibition(image_bytes, n_lanes=6):
    """
    「オリジナル展示」表のスクリーンショット(bytes)から、
    一周・まわり足・直線の3列を読み取り、
    lane(1〜n_lanes) / original_straight / original_turn / original_lap
    のDataFrameを返す。読み取れない場合はNoneを返す。

    場によって表のレイアウトや配色はかなり異なる（直線列が無い場、体重・
    調整・チルトの並び順が違う場、タイムと時速を2段で表示する場、背景色が
    濃くグレースケール変換だけでは文字が潰れる場など）ため、決め打ちの
    座標には頼らず、以下の緩い方式で抽出する。あくまで下書き用の仮入力で
    あり、実際の値は呼び出し側のテーブルで必ず確認・修正してもらう前提。
    一部の艇だけ読み取れなかった場合も、読み取れた艇の分だけは活かせる
    よう、その艇の行は空欄のまま返す（Noneになるのは表自体が読み取れ
    なかった場合のみ）。
    """
    if not OCR_AVAILABLE:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:
        return None

    # --- 0. グレースケールと二値化、より多く数値を拾えた方を採用する ---
    # 場によって配色が大きく異なり、グレースケール変換だけでは文字が
    # 背景に潰れて読めない場合があるため、両方試してより多く数値を
    # 拾えた方を使う。
    variants = [img]
    try:
        variants.append(img.point(lambda p: 255 if p > 90 else 0))
    except Exception:
        pass

    tokens, raw_tokens = [], []
    for variant in variants:
        m, r = _ocr_pass(variant)
        if len(m) > len(tokens):
            tokens, raw_tokens = m, r

    if len(tokens) < max(6, n_lanes):
        return None

    # --- 1. Y座標が近いものをまとめて1行（1艇分）にする ---
    tokens.sort(key=lambda r: r["y"])
    rows = []
    current = []
    last_y = None
    for tok in tokens:
        if last_y is not None and tok["y"] - last_y > _ROW_GAP:
            if current:
                rows.append(current)
            current = []
        current.append(tok)
        last_y = tok["y"]
    if current:
        rows.append(current)

    # 体重・チルトだけの行（列によって基準線が微妙にズレて別行として
    # 分かれてしまったもの）はここで除外し、艇の行番号がズレないようにする。
    candidate_rows = [r for r in rows if _is_meaningful(r)]
    if not candidate_rows:
        return None

    # --- 2. 「タイム／時速」を1艇について2行に分けて表示する場（BOATRACE平和島など）の対策 ---
    # 時速の行は一周・まわり足・直線に相当する値が軒並みタイムの行より
    # 大きくなるため、行数がだいたい2倍あり、かつ交互に値が大きく変わる
    # 場合は「タイム」側（先に出てくる方）の行だけを残す。
    if n_lanes * 1.5 <= len(candidate_rows) <= n_lanes * 2.5:
        def _row_med(r):
            vs = sorted(t["value"] for t in r)
            return vs[len(vs) // 2]

        pair_count = (len(candidate_rows) // 2) * 2
        meds = [_row_med(r) for r in candidate_rows[:pair_count]]
        firsts = meds[0::2]
        seconds = meds[1::2]
        sum_firsts = sum(firsts)
        sum_seconds = sum(seconds)
        if sum_firsts > 0 and sum_seconds > sum_firsts * 1.5:
            candidate_rows = candidate_rows[0::2]
        elif sum_seconds > 0 and sum_firsts > sum_seconds * 1.5:
            candidate_rows = candidate_rows[1::2]

    # 表の途中の艇が丸ごと読み取れないケースは稀で、たいてい一部の艇
    # （特に読み取りづらい艇）が抜けるだけなので、行の並び順=艇番の
    # 順番とみなし、抜けた分は空欄で埋める。1艇も読み取れていなければ
    # 打ち切る。
    candidate_rows = candidate_rows[:n_lanes]

    # --- 3・4. 各行の「一周」候補・除外対象を仕分けし、その場でまだ
    #     「展示タイム」を除外するかどうかは決めず、後段でテーブル全体
    #     の傾向を見てから判断する（一部の艇だけ展示タイムの検出に
    #     失敗した場合に、まわり足を展示タイムと誤認しないようにするため）。
    row_info = []
    for row in candidate_rows:
        used_ids = set()

        lap_pool = [t for t in row if _LAP_MIN <= t["value"] <= _LAP_MAX]
        lap_val = None
        if lap_pool:
            lap_tok = lap_pool[0]
            lap_val = lap_tok["value"]
            used_ids.add(id(lap_tok))
        else:
            fallback_pool = [t for t in row if t["value"] >= _LAP_FALLBACK_MIN]
            if fallback_pool:
                lap_tok = fallback_pool[0]
                lap_val = lap_tok["value"]
                used_ids.add(id(lap_tok))
            else:
                # 列同士の間隔が狭く、体重・チルト・展示タイム・一周が
                # 1つの文字列として繋がって読み取られた場合のフォールバック。
                # その行の近くにある生テキストから数値パターンを正規表現で
                # 拾い、一周らしき範囲(30〜42)に収まる最後の値（＝一番右側
                # にある値）を一周とみなす。
                row_ys = [t["y"] for t in row]
                y_lo = min(row_ys) - _ROW_Y_TOLERANCE
                y_hi = max(row_ys) + _ROW_Y_TOLERANCE
                for r in raw_tokens:
                    if not (y_lo <= r["y"] <= y_hi):
                        continue
                    if len(r["text"]) < 6:
                        continue
                    matches = _NUM_RE_ANY.findall(r["text"])
                    lap_candidates = [float(mm) for mm in matches if _LAP_MIN <= float(mm) <= _LAP_MAX]
                    if lap_candidates:
                        lap_val = lap_candidates[-1]
                        break

        # 体重（一周としては拾われなかった、大きめの値）とチルト・調整
        # （小さい値）に相当する列を除外する。
        rest = [
            t for t in row
            if id(t) not in used_ids
            and t["value"] <= _LAP_MAX
            and not (_TILT_MIN <= t["value"] <= _TILT_MAX)
        ]
        rest_sorted = sorted(rest, key=lambda r: r["x"])
        row_info.append({"lap": lap_val, "rest": rest_sorted})

    # --- テーブル全体から「展示タイム列が存在し、かつ左端に来ている」か
    #     どうかを多数決で判断する。1行ごとにこれを判断すると、たまたま
    #     その行だけ展示タイムの検出に失敗した場合に、まわり足の値を
    #     誤って展示タイムとして除外してしまうため。
    leftmost_in_band = [
        ri["rest"][0]["value"]
        for ri in row_info
        if ri["rest"] and _DISPLAY_MIN <= ri["rest"][0]["value"] <= _DISPLAY_MAX
    ]
    rows_with_data = [ri for ri in row_info if ri["rest"]]
    drop_display = bool(rows_with_data) and len(leftmost_in_band) >= max(1, len(rows_with_data) // 2)

    lengths = [len(ri["rest"]) for ri in row_info if ri["rest"]]
    typical_len = max(set(lengths), key=lengths.count) if lengths else 0

    out_rows = []
    for lane in range(1, n_lanes + 1):
        if lane - 1 >= len(row_info):
            out_rows.append({
                "lane": lane, "original_lap": None,
                "original_turn": None, "original_straight": None,
            })
            continue

        ri = row_info[lane - 1]
        rest_sorted = ri["rest"]

        if drop_display and rest_sorted:
            # 通常はテーブル全体の傾向どおり左端を展示タイムとみなして除外する。
            # ただしこの行だけ検出漏れで想定より値が少ない場合は、
            # 展示タイムそのものが読めていない可能性が高いため、
            # この行の左端が展示タイムらしい範囲に入っている時だけ除外する。
            if len(rest_sorted) >= typical_len or (
                _DISPLAY_MIN <= rest_sorted[0]["value"] <= _DISPLAY_MAX
            ):
                tail = rest_sorted[1:]
            else:
                tail = rest_sorted
        else:
            tail = rest_sorted

        if len(tail) > 2:
            tail = tail[-2:]

        turn_val = tail[0]["value"] if len(tail) >= 1 else None
        straight_val = tail[1]["value"] if len(tail) >= 2 else None

        out_rows.append({
            "lane": lane,
            "original_lap": ri["lap"],
            "original_turn": turn_val,
            "original_straight": straight_val,
        })

    return pd.DataFrame(out_rows)
