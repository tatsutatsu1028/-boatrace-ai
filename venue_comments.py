from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
}

GAMAGORI_COMMENT_ALL = (
    "https://www.gamagori-kyotei.com/asp/gamagori/sp/kyogi/"
    "kyogihtml/comment_all/comment_all07.htm"
)

# 蒲郡公式サイト自身が案内している「出場選手コメントボード」の掲載先
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


def _looks_like_comment(s):
    s = _norm(s)
    if len(s) < 6 or len(s) > 320:
        return False

    # 数値表・UI文字列を除外
    ng = (
        "コメント&モーター一覧", "コメント＆モーター一覧",
        "通算", "近況", "パワー", "勝率", "2連率",
        "モーター", "ボート", "登録番号", "選手名", "級別",
        "ライブ", "リプレイ", "投票", "MENU",
    )
    if any(x in s for x in ng) and len(s) < 40:
        return False

    jp = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))
    if jp < 5:
        return False

    # コメントで頻出する語を優先
    key = (
        "足", "伸び", "出足", "回り", "まわり", "乗り",
        "スタート", "ターン", "気配", "エンジン", "モーター",
        "調整", "ペラ", "行き足", "直線", "悪く", "良く",
        "いい", "欲しい", "重い", "軽い", "普通",
    )
    return any(k in s for k in key)


def _best_sentence(block):
    block = _norm(block)

    # JSONやHTML由来のエスケープを軽く戻す
    block = (
        block.replace("\\n", " ")
             .replace("\\r", " ")
             .replace('\\"', '"')
             .replace("\\/", "/")
    )

    # 「。」が無い短文もあるので、句点・改行相当・引用符で候補分割
    parts = re.split(r"(?<=[。！？!?])\s+|[|｜]{1,2}", block)
    candidates = []

    for p in parts:
        p = _norm(p).strip(" \"'[]{}(),")
        if not _looks_like_comment(p):
            continue

        score = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", p))
        for k in ("足", "伸び", "出足", "回り", "乗り", "スタート", "ターン", "気配"):
            if k in p:
                score += 12
        candidates.append((score, p))

    return max(candidates, default=(0, ""))[1]


def _visible_text_and_scripts(html):
    soup = BeautifulSoup(html, "lxml")
    visible = _norm(soup.get_text(" ", strip=True))

    # LINE VOOM等は本文をJS/JSON内に持つことがあるのでscriptも検索対象へ。
    scripts = []
    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text(" ", strip=True)
        if txt:
            scripts.append(txt)

    raw = "\n".join(scripts)

    # \uXXXX を含むJSON文字列を読める範囲でデコード
    try:
        raw = bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        pass

    return visible, _norm(raw)


def _extract_comments_from_document(html, race):
    out = {}
    visible, scripts = _visible_text_and_scripts(html)

    # まず普通のHTML本文、次に埋め込みJSON/JS
    for idx, row in race.iterrows():
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue

        for doc in (visible, scripts):
            block = _extract_between_names(doc, race, name)
            if not block:
                continue
            c = _best_sentence(block)
            if c:
                out[int(row["lane"])] = c
                break

    return out


def fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race):
    """
    蒲郡(07)の選手コメント取得。

    優先順位:
      1. 蒲郡公式が案内している LINE VOOM「出場選手コメントボード」
      2. 蒲郡公式「コメント＆モーター一覧」に実コメントが存在する場合
      3. 取得できなければ空欄（BOAT RACE本体 pitreport / 手入力へフォールバック）

    注意:
    「コメント＆モーター一覧」というページ名でも、
    開催によっては選手コメント本文がHTMLに存在せず、
    モーター指標だけが掲載されていることがあります。
    その場合、数値表をコメントと誤認しません。
    """
    out = _empty(race)

    # 1) LINE VOOM
    try:
        html = _get(GAMAGORI_LINE_VOOM)
        found = _extract_comments_from_document(html, race)
        for idx, row in race.iterrows():
            lane = int(row["lane"])
            if lane in found:
                out.loc[idx, "venue_comment"] = found[lane]
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・出場選手コメントボード"
                out.loc[idx, "venue_comment_url"] = GAMAGORI_LINE_VOOM
    except Exception:
        pass

    # 6艇揃えば終了
    if (out["venue_comment"].astype(str).str.strip() != "").sum() == 6:
        return out

    # 2) コメント＆モーター一覧
    try:
        html = _get(GAMAGORI_COMMENT_ALL)
        found = _extract_comments_from_document(html, race)

        for idx, row in race.iterrows():
            if _norm(out.loc[idx, "venue_comment"]):
                continue
            lane = int(row["lane"])
            if lane in found:
                out.loc[idx, "venue_comment"] = found[lane]
                out.loc[idx, "venue_comment_source"] = "蒲郡公式・コメント＆モーター一覧"
                out.loc[idx, "venue_comment_url"] = GAMAGORI_COMMENT_ALL
    except Exception:
        pass

    return out


def fetch_tsu_comments(date_yyyymmdd, jcd, rno, race):
    """
    津(09)公式サイトの選手コメントを取得。
    """
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

        if comment:
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "津公式・選手コメント"
            out.loc[idx, "venue_comment_url"] = url

    return out


def fetch_venue_comments(date_yyyymmdd, jcd, rno, race):
    """
    場別コメント取得ルーター。
    """
    code = str(jcd).zfill(2)

    try:
        if code == "07":
            return fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race)
        if code == "09":
            return fetch_tsu_comments(date_yyyymmdd, jcd, rno, race)
    except Exception:
        pass

    return _empty(race)
