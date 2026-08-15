from __future__ import annotations

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

# 蒲郡公式の「コメント＆モーター一覧」
# このページはモーター表が主体で、自然文コメントが無い開催もある。
GAMAGORI_COMMENT_ALL = (
    "https://www.gamagori-kyotei.com/asp/gamagori/sp/kyogi/"
    "kyogihtml/comment_all/comment_all07.htm"
)

# 蒲郡公式が案内している出場選手コメントボード
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

    # unicode escapeが含まれていれば軽く戻す
    try:
        raw = bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        pass

    return visible, _norm(raw)


def _extract_between_names(text, race, target_name, max_chars=1200):
    text = _norm(text)
    pat = _name_regex(target_name)
    if not pat:
        return ""

    m = re.search(pat, text)
    if not m:
        return ""

    start = m.end()
    end = min(len(text), start + max_chars)

    for nm in race["racer_name"].fillna("").astype(str):
        if _compact_name(nm) == _compact_name(target_name):
            continue
        p = _name_regex(nm)
        if not p:
            continue
        m2 = re.search(p, text[start:end])
        if m2:
            end = min(end, start + m2.start())

    return _norm(text[start:end])


def _looks_like_motor_table_noise(s):
    """
    蒲郡「コメント＆モーター一覧」の表全体を
    選手コメントとして誤認するケースを強く除外する。
    """
    s = _norm(s)

    # 数字が多すぎる文章は表データとみなす
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    digits = sum(ch.isdigit() for ch in s)

    # B1) 57 0.00 0.00 ... のようなパターン
    if re.search(r"\b[AB][12]\)?\s+\d+", s):
        return True

    # 登番が何人も並ぶパターン
    if len(re.findall(r"\b\d{4}\b", s)) >= 2:
        return True

    # 表の見出しが大量に混ざる
    table_words = (
        "通算", "近況", "パワー", "出足", "伸び", "回り足",
        "勝率", "2連率", "モーター", "ボート", "登録番号",
        "級別", "No.", "NO.", "No",
    )
    table_hit = sum(1 for w in table_words if w in s)

    # 数字だらけ・表語が多い
    if len(nums) >= 8:
        return True
    if digits >= 14:
        return True
    if table_hit >= 4:
        return True

    # 0.00 が何度も並ぶ
    if s.count("0.00") >= 2:
        return True

    return False


def _looks_like_natural_comment(s):
    s = _norm(s)

    if not (6 <= len(s) <= 220):
        return False

    if _looks_like_motor_table_noise(s):
        return False

    jp_count = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))
    if jp_count < 6:
        return False

    # 自然文らしい助詞・終止表現
    natural_markers = (
        "は", "が", "けど", "けれど", "と思う", "と思います",
        "です", "ます", "ない", "いい", "悪い", "良い",
        "感じ", "雰囲気", "欲しい", "している", "なった",
    )
    marker_hits = sum(1 for x in natural_markers if x in s)

    # コメントでよく使う機力語
    race_words = (
        "足", "伸び", "出足", "回り", "まわり", "乗り",
        "スタート", "ターン", "気配", "エンジン", "モーター",
        "調整", "ペラ", "行き足", "直線", "押し", "舟足",
    )
    race_hits = sum(1 for x in race_words if x in s)

    # 自然文っぽさ＋競艇語のどちらかを要求
    return marker_hits >= 1 and race_hits >= 1


def _best_sentence(block):
    block = _norm(block)
    if not block:
        return ""

    block = (
        block.replace("\\n", " ")
             .replace("\\r", " ")
             .replace('\\"', '"')
             .replace("\\/", "/")
    )

    # 先に明らかな表ノイズを弾く
    if _looks_like_motor_table_noise(block):
        # 全体は表でも、自然文が途中に紛れている可能性があるので分割して試す
        pass

    # 文として切り出す
    parts = re.split(
        r"(?<=[。！？!?])\s+|[|｜]{1,2}|(?<=。)|(?<=！)|(?<=？)",
        block
    )

    candidates = []
    for p in parts:
        p = _norm(p).strip(" \"'[]{}(),")
        if not _looks_like_natural_comment(p):
            continue

        score = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", p))
        for k in (
            "足", "伸び", "出足", "回り", "乗り", "スタート",
            "ターン", "気配", "行き足", "直線"
        ):
            if k in p:
                score += 10

        # 数字が多いものは減点
        score -= len(re.findall(r"\d+(?:\.\d+)?", p)) * 6
        candidates.append((score, p))

    return max(candidates, default=(0, ""))[1]


def _extract_comments_from_document(html, race):
    out = {}
    visible, scripts = _visible_text_and_scripts(html)

    for _, row in race.iterrows():
        lane = int(row["lane"])
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue

        for doc in (visible, scripts):
            block = _extract_between_names(doc, race, name)
            if not block:
                continue

            c = _best_sentence(block)
            if c:
                out[lane] = c
                break

    return out


def fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race):
    """
    蒲郡(07)選手コメント取得。

    重要:
    「コメント＆モーター一覧」のモーター表を
    コメント本文として誤採用しない。

    優先順位:
      1) 蒲郡公式が案内する LINE VOOM の出場選手コメント
      2) コメント＆モーター一覧に自然文コメントが本当に存在する場合のみ採用
      3) 無ければ空欄で返す
    """
    out = _empty(race)

    # 1) LINE VOOM
    try:
        html = _get(GAMAGORI_LINE_VOOM)
        found = _extract_comments_from_document(html, race)

        for idx, row in race.iterrows():
            lane = int(row["lane"])
            c = found.get(lane, "")
            if c and _looks_like_natural_comment(c):
                out.loc[idx, "venue_comment"] = c
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・出場選手コメントボード"
                out.loc[idx, "venue_comment_url"] = GAMAGORI_LINE_VOOM
    except Exception:
        pass

    if (out["venue_comment"].astype(str).str.strip() != "").sum() == 6:
        return out

    # 2) コメント＆モーター一覧
    # モーター表の誤抽出を防ぐため、自然文コメントだけ通す。
    try:
        html = _get(GAMAGORI_COMMENT_ALL)
        found = _extract_comments_from_document(html, race)

        for idx, row in race.iterrows():
            if _norm(out.loc[idx, "venue_comment"]):
                continue

            lane = int(row["lane"])
            c = found.get(lane, "")

            if c and _looks_like_natural_comment(c):
                out.loc[idx, "venue_comment"] = c
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・コメント＆モーター一覧"
                out.loc[idx, "venue_comment_url"] = GAMAGORI_COMMENT_ALL
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
        name = _norm(row.get("racer_name", ""))
        block = _extract_between_names(page, race, name, max_chars=900)
        if not block:
            continue

        comment = ""
        for dt in date_tokens:
            m = re.search(
                re.escape(dt) + r"\s*(.+?)(?=\s*\d{4}/\d{1,2}/\d{1,2}\s*|$)",
                block,
            )
            if m:
                comment = _best_sentence(m.group(1))
                if comment:
                    break

        if not comment:
            comment = _best_sentence(block)

        if comment and _looks_like_natural_comment(comment):
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "津公式・選手コメント"
            out.loc[idx, "venue_comment_url"] = url

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
