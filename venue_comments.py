from __future__ import annotations

import io
from pypdf import PdfReader

import html as html_lib
import re
import unicodedata
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

GAMAGORI_COMMENT_ALL = (
    "https://www.gamagori-kyotei.com/asp/gamagori/sp/kyogi/"
    "kyogihtml/comment_all/comment_all07.htm"
)

GAMAGORI_LINE_VOOM = (
    "https://linevoom.line.me/user/"
    "_dUn_YlpPps1E9pNPqbT9isaymloPeRIYud9x4l4"
)


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _compact_name(x):
    return re.sub(r"\s+", "", _norm(x))


def _get(url):
    r = requests.get(
        url,
        headers={
            **UA,
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.6",
        },
        timeout=25,
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _empty(race):
    out = race[["lane", "racer_name"]].copy()
    out["venue_comment"] = ""
    out["venue_comment_source"] = ""
    out["venue_comment_url"] = ""
    return out


def _name_regex(name):
    n = _compact_name(name)
    if not n:
        return None
    return r"\s*".join(re.escape(ch) for ch in n)


def _visible_text_and_scripts(html):
    soup = BeautifulSoup(html, "lxml")
    visible = _norm(soup.get_text(" ", strip=True))

    scripts = []
    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text(" ", strip=True)
        if txt:
            scripts.append(txt)

    raw = "\n".join(scripts)
    try:
        raw = bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        pass

    return visible, _norm(raw)


def _all_name_positions(text, race):
    found = []
    for _, row in race.iterrows():
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue
        pat = _name_regex(name)
        if not pat:
            continue
        for m in re.finditer(pat, text):
            found.append((m.start(), m.end(), int(row["lane"]), name))
    return sorted(found, key=lambda x: x[0])


def _candidate_blocks(text, race, lane, name):
    """
    同じ選手名がページ中に何度も出る前提で、
    すべての出現位置から「次の選手名まで」を候補化する。
    蒲郡ページは選手一覧/モーター表/コメント部で名前が重複するため、
    最初の1個だけを見るとモーター表を拾いやすい。
    """
    text = _norm(text)
    positions = _all_name_positions(text, race)
    blocks = []

    for i, (start, end, ln, nm) in enumerate(positions):
        if ln != int(lane):
            continue

        next_start = len(text)
        for j in range(i + 1, len(positions)):
            if positions[j][0] > end:
                next_start = positions[j][0]
                break

        # コメントは名前の直後にある想定。
        # ただし巨大ブロック化しないよう上限を設ける。
        cut = min(next_start, end + 700)
        block = _norm(text[end:cut])
        if block:
            blocks.append(block)

    return blocks


def _looks_like_motor_table_noise(s):
    s = _norm(s)

    nums = re.findall(r"\d+(?:\.\d+)?", s)
    digits = sum(ch.isdigit() for ch in s)

    if re.search(r"\b[AB][12]\)?\s+\d+", s):
        return True

    if len(re.findall(r"\b\d{4}\b", s)) >= 2:
        return True

    if s.count("0.00") >= 2:
        return True

    table_words = (
        "通算", "近況", "パワー", "勝率", "2連率",
        "登録番号", "級別", "No.", "NO."
    )
    table_hits = sum(1 for w in table_words if w in s)

    if len(nums) >= 8:
        return True
    if digits >= 14:
        return True
    if table_hits >= 3:
        return True

    return False


def _looks_like_natural_comment(s):
    s = _norm(s)

    if not (5 <= len(s) <= 180):
        return False

    if _looks_like_motor_table_noise(s):
        return False

    jp = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))
    if jp < 5:
        return False
 # 選手成績・モーター一覧などの表データを除外する
    nums = re.findall(r"\d+(?:\.\d+)?", s)

    table_words = (
        "登録番号",
        "級別",
        "勝率/2連率",
        "勝率",
        "2連率",
        "通算",
        "近況",
        "モーター一覧",
        "コメント&モーター",
        "コメント＆モーター",
    )

    # 数字が大量に並ぶ文章はコメントではなく表データの可能性が高い
    if len(nums) >= 4:
        return False

    # B1 / A1 などの級別と複数の数値が混在する場合も除外
    if re.search(r"(?:^|\s)[AB][12](?:\s|$|\))", s) and len(nums) >= 2:
        return False

    # 成績表特有の見出しが複数含まれる文章を除外
    table_hits = sum(1 for k in table_words if k in s)
    if table_hits >= 2:
        return False

    # 文章全体に数字が多すぎるものも除外
    digit_count = sum(ch.isdigit() for ch in s)
    if digit_count >= 8 and digit_count / max(len(s), 1) > 0.12:
        return False
    race_words = (
        "足", "伸び", "出足", "回り", "まわり", "乗り",
        "スタート", "ターン", "気配", "エンジン", "モーター",
        "調整", "ペラ", "行き足", "直線", "押し", "舟足",
        "走り", "水準", "違和感"
    )
    natural_words = (
        "は", "が", "けど", "けれど", "と思う", "です", "ます",
        "ない", "いい", "良い", "悪い", "感じ", "欲しい",
        "している", "なった", "ならない", "来なかった"
    )

    return (
        any(k in s for k in race_words)
        and any(k in s for k in natural_words)
    )


def _split_sentences(block):
    block = _norm(block)
    block = (
        block.replace("\\n", " ")
        .replace("\\r", " ")
        .replace('\\"', '"')
        .replace("\\/", "/")
    )

    # 表見出しを境に切る
    block = re.sub(
        r"(No\.?|通算|近況|パワー|勝率/2連率|登録番号|級別)",
        " | ",
        block
    )

    parts = re.split(
        r"(?<=[。！？!?])\s*|[|｜]{1,2}",
        block
    )

    return [_norm(p).strip(" \"'[]{}(),") for p in parts if _norm(p)]


def _score_comment(s):
    if not _looks_like_natural_comment(s):
        return -10_000

    score = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))

    for k in (
        "足", "伸び", "出足", "回り", "まわり足", "乗り",
        "スタート", "ターン", "行き足", "直線", "水準",
        "違和感", "良く", "悪く"
    ):
        if k in s:
            score += 12

    # 数字は自然文コメントでは少ないほどよい
    score -= len(re.findall(r"\d+(?:\.\d+)?", s)) * 8

    # 「0.00」等は強く減点
    score -= s.count("0.00") * 50

    return score


def _best_comment_for_racer(document, race, lane, name):
    candidates = []

    for block in _candidate_blocks(document, race, lane, name):
        # ブロック全体も候補
        if _looks_like_natural_comment(block):
            candidates.append((_score_comment(block), block))

        # 文単位でも候補
        for p in _split_sentences(block):
            if _looks_like_natural_comment(p):
                candidates.append((_score_comment(p), p))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _extract_comments_from_document(html, race):
    out = {}
    visible, scripts = _visible_text_and_scripts(html)

    for _, row in race.iterrows():
        lane = int(row["lane"])
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue

        best = ""

        for doc in (visible, scripts):
            c = _best_comment_for_racer(doc, race, lane, name)
            if c and _score_comment(c) > _score_comment(best):
                best = c

        if best:
            out[lane] = best

    return out

def _fetch_gamagori_pdf_comments(date_yyyymmdd, rno, race):
    """
    蒲郡公式「ガマスポ通常版PDF」から
    指定レースの6艇コメントを取得する。
    """
    out = _empty(race)

    pdf_url = (
        "https://www.gamagori-kyotei.com/asp/gamagori/kyogi/"
        "kyogihtml/pdf_A3/"
        f"pdf_A3{date_yyyymmdd}07.pdf"
    )

    try:
        r = requests.get(pdf_url, headers=UA, timeout=20)
        r.raise_for_status()

        reader = PdfReader(io.BytesIO(r.content))
        text = "\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

        text = unicodedata.normalize("NFKC", text)
    # 3) HTMLで取れなかった艇だけ
    #    蒲郡公式「ガマスポPDF」から補完
    if (out["venue_comment"].astype(str).str.strip() == "").any():
        try:
            pdf_out = _fetch_gamagori_pdf_comments(
                date_yyyymmdd,
                rno,
                race,
            )

            for idx in out.index:
                if _norm(out.loc[idx, "venue_comment"]):
                    continue

                c = _norm(pdf_out.loc[idx, "venue_comment"])

                if c:
                    out.loc[idx, "venue_comment"] = c
                    out.loc[idx, "venue_comment_source"] = pdf_out.loc[
                        idx, "venue_comment_source"
                    ]
                    out.loc[idx, "venue_comment_url"] = pdf_out.loc[
                        idx, "venue_comment_url"
                    ]

        except Exception:
            pass
    except Exception:
        return out

    # 指定Rだけを切り出す
    start_pat = re.compile(
        rf"(?m)^\s*{int(rno)}R\s*$"
    )
    start = start_pat.search(text)

    if not start:
        return out

    if int(rno) < 12:
        end_pat = re.compile(
            rf"(?m)^\s*{int(rno) + 1}R\s*$"
        )
        end = end_pat.search(text, start.end())
        block = text[start.start():end.start()] if end else text[start.start():]
    else:
        block = text[start.start():]

    # レース内の「コメント」より後だけを見る
    comment_pos = block.rfind("コメント")
    if comment_pos < 0:
        return out

    comment_block = block[comment_pos + len("コメント"):]

    lines = []
    for line in comment_block.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    for idx, row in race.iterrows():
        lane = int(row["lane"])
        name = _norm(row.get("racer_name", ""))

        if not name:
            continue

        # 姓だけ取り出す
        parts = name.split()
        surname = parts[0] if parts else name

        # PDFでは
        # 1 安田 コメント...
        # S .15 2 佐藤 コメント...
        # のような形式になる
        pat = re.compile(
            rf"^(?:[SD]\s*\.\d+\s*)?"
            rf"{lane}\s+{re.escape(surname)}\s+(.+)$"
        )

        for line in lines:
            m = pat.search(line)

            if not m:
                continue

            comment = _norm(m.group(1))

            if comment and _looks_like_natural_comment(comment):
                out.loc[idx, "venue_comment"] = comment
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・ガマスポPDF"
                out.loc[idx, "venue_comment_url"] = pdf_url
                break

    return out
def fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race):
    """
    蒲郡(07)の選手コメント。

    蒲郡公式「コメント＆モーター一覧」は、
    ページ内に選手名が複数回現れる構造なので、
    各選手名の全出現箇所を調べ、
    その中から自然文コメントだけを採用する。

    モーター表の数値列はコメントとして採用しない。
    """
    out = _empty(race)

    # 1) 蒲郡公式「コメント＆モーター一覧」
    try:
        html = _get(GAMAGORI_COMMENT_ALL)
        

        for idx, row in race.iterrows():
            lane = int(row["lane"])
            c = found.get(lane, "")
            if c and _looks_like_natural_comment(c):
                out.loc[idx, "venue_comment"] = c
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・コメント＆モーター一覧"
                out.loc[idx, "venue_comment_url"] = GAMAGORI_COMMENT_ALL
    except Exception:
        pass

    # 2) 未取得分だけLINE VOOMも試す
    if (out["venue_comment"].astype(str).str.strip() != "").sum() < 6:
        try:
            html = _get(GAMAGORI_LINE_VOOM)
            found = _extract_comments_from_document(html, race)

            for idx, row in race.iterrows():
                if _norm(out.loc[idx, "venue_comment"]):
                    continue
                lane = int(row["lane"])
                c = found.get(lane, "")
                if c and _looks_like_natural_comment(c):
                    out.loc[idx, "venue_comment"] = c
                    out.loc[idx, "venue_comment_source"] = "蒲郡公式・出場選手コメントボード"
                    out.loc[idx, "venue_comment_url"] = GAMAGORI_LINE_VOOM
        except Exception:
            pass

    return out


def fetch_tsu_comments(date_yyyymmdd, jcd, rno, race):
    out = _empty(race)
    url = f"https://www.boatrace-tsu.com/sp/index.php?page=yosou-yosou&race={int(rno)}"

    try:
        html = _get(url)
    except Exception:
        return out

    visible, scripts = _visible_text_and_scripts(html)
    page = visible + " " + scripts

    req_date = datetime.strptime(str(date_yyyymmdd), "%Y%m%d")
    date_tokens = (
        f"{req_date.year}/{req_date.month}/{req_date.day}",
        f"{req_date.year}/{req_date.month:02d}/{req_date.day:02d}",
    )

    for idx, row in race.iterrows():
        lane = int(row["lane"])
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue

        comment = _best_comment_for_racer(page, race, lane, name)

        # 日付付きコメントがあればそちらを優先
        blocks = _candidate_blocks(page, race, lane, name)
        for block in blocks:
            for dt in date_tokens:
                m = re.search(
                    re.escape(dt) + r"\s*(.+?)(?=\s*\d{4}/\d{1,2}/\d{1,2}\s*|$)",
                    block,
                )
                if m:
                    dated = ""
                    for p in _split_sentences(m.group(1)):
                        if _looks_like_natural_comment(p):
                            if not dated or _score_comment(p) > _score_comment(dated):
                                dated = p
                    if dated:
                        comment = dated
                        break

        if comment and _looks_like_natural_comment(comment):
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "津公式・選手コメント"
            out.loc[idx, "venue_comment_url"] = url

        # 3) HTMLで取れなかった艇だけ
    #    蒲郡公式「ガマスポPDF」から補完
    if (out["venue_comment"].astype(str).str.strip() == "").any():
        try:
            pdf_out = _fetch_gamagori_pdf_comments(
                date_yyyymmdd,
                rno,
                race,
            )

            for idx in out.index:
                if _norm(out.loc[idx, "venue_comment"]):
                    continue

                c = _norm(pdf_out.loc[idx, "venue_comment"])

                if c:
                    out.loc[idx, "venue_comment"] = c
                    out.loc[idx, "venue_comment_source"] = pdf_out.loc[
                        idx, "venue_comment_source"
                    ]
                    out.loc[idx, "venue_comment_url"] = pdf_out.loc[
                        idx, "venue_comment_url"
                    ]

        except Exception:
            pass
    return out


def fetch_venue_comments(date_yyyymmdd, jcd, rno, race):
    code = str(jcd).zfill(2)

    try:
        if code == "07":
            return fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race)
        if code == "09":
            return fetch_tsu_comments(date_yyyymmdd, jcd, rno, race)
    except Exception:
        pass

    return _empty(race)
