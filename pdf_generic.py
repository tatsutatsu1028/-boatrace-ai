"""
会場ごとに作りの違う出走表PDFから、「何Rの何号艇が誰か（登録番号）」と締切時刻だけを読み取る（共通の読み方）。

どの会場のPDFにも載っている登録番号を手がかりにする:
  1) 手元の過去データにある登録番号（4桁）の語の位置を集める
  2) 同じ列（x がほぼ同じ）で縦に並ぶ番号を、行の間隔で区切り、ちょうど6人のまとまりを1レースとみなす
     （上から1号艇〜6号艇。表紙の選手一覧などの長い並びは6人ではないので捨てる）
  3) まとまりのすぐ上にある締切時刻（HH:MM）を拾い、締切の早い順にレース番号を付ける
選手の成績は PDF からは読まず、手元の過去データの最新の値を使う（pdf_predict_generic.py）。
"""
from __future__ import annotations

import re
import statistics
import unicodedata

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _z2h(s):
    return unicodedata.normalize("NFKC", str(s))


def _times(ws):
    out = []
    for w in ws:
        t = _z2h(w["text"]).replace("：", ":")
        m = TIME_RE.match(t)
        if m and 7 <= int(m.group(1)) <= 21:
            out.append((w, f"{int(m.group(1)):02d}:{m.group(2)}"))
    return out


def page_blocks(page, known_ids):
    ws = page.extract_words(extra_attrs=["size"])  # 文字の大きさが違う語をくっつけない
    # 手元に無い登録番号（新人など）も拾い、あとで「6人中4人以上が既知」のまとまりだけ使う
    ids = [w for w in ws if re.fullmatch(r"\d{4}", w["text"]) and 2000 < int(w["text"]) < 6000]
    times = _times(ws)
    # 列ごと（x0 が 12pt 以内）にまとめる
    cols = []
    for w in sorted(ids, key=lambda w: w["x0"]):
        for c in cols:
            if abs(c[0]["x0"] - w["x0"]) < 12:
                c.append(w)
                break
        else:
            cols.append([w])
    blocks = []
    for c in cols:
        c = sorted(c, key=lambda w: w["top"])
        if len(c) < 6:
            continue
        gaps = [b["top"] - a["top"] for a, b in zip(c, c[1:])]
        pitch = statistics.median(gaps)
        runs, cur = [], [c[0]]
        for g, w in zip(gaps, c[1:]):
            if g < 1.6 * pitch and g > 0.5 * pitch:
                cur.append(w)
            else:
                runs.append(cur)
                cur = [w]
        runs.append(cur)
        # 表の下の別の一覧などで7〜8個つながったときは、先頭の6人だけを1レースとして扱う
        runs = [r[:6] for r in runs if 6 <= len(r) <= 8]
        for r in runs:
            if sum(w["text"] in known_ids for w in r) < 4:
                continue
            top, x = r[0]["top"], r[0]["x0"]
            # まとまりの上（1.5行〜6行ぶん上まで）で、横に近い締切時刻
            cand = [(top - w["top"], abs(w["x0"] - x), t) for w, t in times
                    if 0 < top - w["top"] < pitch * 6 and abs(w["x0"] - x) < 260]
            if not cand:
                continue
            cand.sort(key=lambda c: (c[0] // pitch, c[1]))
            blocks.append({"deadline": cand[0][2], "ids": [w["text"] for w in r], "top": top, "x": x})
    return blocks


def read_venue(paths, known_ids):
    """1会場の出走表PDF（複数可）→ [{"race_no", "deadline", "ids"}]（締切順に1R〜）。"""
    import pdfplumber

    blocks = []
    for p in paths:
        with pdfplumber.open(p) as pdf:
            for page in pdf.pages:
                blocks += page_blocks(page, known_ids)
    seen, uniq = set(), []
    for b in sorted(blocks, key=lambda b: b["deadline"]):
        key = (b["deadline"], tuple(b["ids"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    return [{"race_no": i, "deadline": b["deadline"], "ids": b["ids"]} for i, b in enumerate(uniq, start=1)]
