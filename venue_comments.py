from __future__ import annotations

import re
import unicodedata
from datetime import datetime
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.6; personal-analysis-tool)"
}

def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()

def _compact_name(x):
    return re.sub(r"\s+", "", _norm(x))

def _get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def _empty(race):
    out = race[["lane", "racer_name"]].copy()
    out["venue_comment"] = ""
    out["venue_comment_source"] = ""
    out["venue_comment_url"] = ""
    return out

def _extract_name_window(text, names, target_name, max_chars=420):
    """
    ページ全文から対象選手名の直後〜次の出走選手名までを切り出す。
    HTML構造変更に比較的強いフォールバック用。
    """
    t = _norm(text)
    target = _compact_name(target_name)
    if not target:
        return ""

    # 全選手名をスペース無しでも検索できるようページ側も詰めると位置が崩れるため、
    # 表記揺れ用に姓・名の間の空白だけ許可した正規表現を作る。
    chars = list(target)
    pat = r"\s*".join(map(re.escape, chars))
    m = re.search(pat, t)
    if not m:
        return ""

    start = m.end()
    end = min(len(t), start + max_chars)

    # 他の出走選手名が出たらそこで止める
    for nm in names:
        nm2 = _compact_name(nm)
        if not nm2 or nm2 == target:
            continue
        p2 = r"\s*".join(map(re.escape, list(nm2)))
        m2 = re.search(p2, t[start:end])
        if m2:
            end = min(end, start + m2.start())

    return _norm(t[start:end])

def _clean_comment(s):
    s = _norm(s)
    # UI見出しや成績類をある程度除去
    s = re.sub(r"^(?:選手コメント|コメント|選手コメント履歴)\s*", "", s)
    s = re.sub(r"\b\d{4}/\d{1,2}/\d{1,2}\b", "", s)
    s = re.sub(r"\b\d{4}\s*/\s*[AB][12]\b.*?$", "", s)
    return _norm(s)

def fetch_tsu_comments(date_yyyymmdd, jcd, rno, race):
    """
    津(09)公式サイトのレース予想ページから選手コメントを取得。
    現在の津公式ページには選手ごとのコメント履歴が掲載される。
    """
    out = _empty(race)
    url = f"https://www.boatrace-tsu.com/sp/index.php?page=yosou-yosou&race={int(rno)}"
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")
    page = _norm(soup.get_text(" ", strip=True))
    names = race["racer_name"].fillna("").astype(str).tolist()

    req_date = datetime.strptime(str(date_yyyymmdd), "%Y%m%d")
    date_tokens = {
        f"{req_date.year}/{req_date.month}/{req_date.day}",
        f"{req_date.year}/{req_date.month:02d}/{req_date.day:02d}",
    }

    for idx, row in race.iterrows():
        name = _norm(row.get("racer_name", ""))
        window = _extract_name_window(page, names, name, max_chars=650)
        if not window:
            continue

        # 指定日のコメントを優先
        comment = ""
        for dt in date_tokens:
            m = re.search(
                re.escape(dt) + r"\s*(.+?)(?=\s*\d{4}/\d{1,2}/\d{1,2}\s*|$)",
                window,
            )
            if m:
                comment = _clean_comment(m.group(1))
                break

        # 日付区切りが取れない場合は最初のコメントらしい文を使用
        if not comment:
            m = re.search(
                r"(?:コメント履歴)?\s*(?:\d{4}/\d{1,2}/\d{1,2}\s*)?(.{8,220}?[。！？])",
                window,
            )
            if m:
                comment = _clean_comment(m.group(1))

        if comment:
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "津公式・選手コメント"
            out.loc[idx, "venue_comment_url"] = url

    return out

def fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race):
    """
    蒲郡(07)公式「コメント＆モーター一覧」から選手コメントを取得。
    レース出走選手名で開催全体のコメント一覧と照合する。
    """
    out = _empty(race)

    # PC/SPでURL構成が固定されているため、まずspを使用。
    url = "https://www.gamagori-kyotei.com/asp/gamagori/sp/kyogi/kyogihtml/comment_all/comment_all07.htm"
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")
    page = _norm(soup.get_text(" ", strip=True))
    names = race["racer_name"].fillna("").astype(str).tolist()

    noise = (
        "通算", "近況", "パワー", "出足", "伸び", "回り足",
        "勝率", "2連率", "モーター", "コメント＆モーター一覧"
    )

    for idx, row in race.iterrows():
        name = _norm(row.get("racer_name", ""))
        window = _extract_name_window(page, names, name, max_chars=520)
        if not window:
            continue

        # 名前の後ろにある、数値表ではない日本語文を拾う。
        sentences = re.findall(r"([一-龥々ぁ-んァ-ヶーA-Za-z0-9・、，,.「」『』（）()ー\s]{8,240}?[。！？])", window)
        candidates = []
        for s in sentences:
            s = _clean_comment(s)
            if not s:
                continue
            if any(n in s for n in noise) and len(s) < 35:
                continue
            jp = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))
            if jp >= 6:
                score = jp
                if any(k in s for k in ("足", "伸び", "出足", "回り", "乗り", "スタート", "気配", "ターン")):
                    score += 20
                candidates.append((score, s))

        if candidates:
            comment = max(candidates, key=lambda x: x[0])[1]
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "蒲郡公式・コメント＆モーター"
            out.loc[idx, "venue_comment_url"] = url

    return out

def fetch_venue_comments(date_yyyymmdd, jcd, rno, race):
    """
    場別コメント取得ルーター。

    現在の専用対応:
      07 蒲郡
      09 津

    未対応場は空欄を返し、official_fetcher側で
    BOAT RACE本体 pitreport → 手入力へフォールバックする。
    """
    code = str(jcd).zfill(2)

    try:
        if code == "07":
            return fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race)
        if code == "09":
            return fetch_tsu_comments(date_yyyymmdd, jcd, rno, race)
    except Exception:
        # 場別サイトの仕様変更でアプリ全体を止めない
        pass

    return _empty(race)
