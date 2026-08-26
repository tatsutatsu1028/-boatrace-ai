from __future__ import annotations

import re
import unicodedata

import warnings

import pandas as pd
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from official_fetcher import VENUES as VENUES_LABEL

# 公式ページのレスポンスがXML宣言を含むことがあり、lxmlパーサーで
# HTMLとして読むと毎回警告が出てログが読みにくくなるため抑制する。
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

BASE = "https://www.boatrace.jp/owpc/pc/race/index"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/3.2; personal-analysis-tool)"
}

VENUE_CODES = [f"{i:02d}" for i in range(1, 25)]


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(date_yyyymmdd):
    url = f"{BASE}?hd={date_yyyymmdd}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text, url


def _parse_venue_block(text):
    """
    会場1枠分のブロックテキストから、開催日目・状態を読み取る。

    公式サイトのマークアップ変更に弱い可能性があるため、
    厳密な位置指定ではなく緩いキーワード・正規表現マッチで抽出する。
    見つからない項目は空のまま返し、呼び出し側でフォールバック表示にする。
    """
    day_label = ""
    m = re.search(r"(初日|最終日|\d{1,2}日目)", text)
    if m:
        day_label = m.group(1)

    status = ""
    next_race_no = None
    next_race_time = ""

    if re.search(r"最終R発売終了|本日は非開催|開催終了", text):
        status = "開催終了"
    else:
        m2 = re.search(r"(\d{1,2})R\s*(\d{1,2}:\d{2})", text)
        if m2:
            next_race_no = int(m2.group(1))
            next_race_time = m2.group(2)
            status = f"{next_race_no}R {next_race_time}"
        elif "発売開始前" in text:
            status = "発売開始前"

    return day_label, status, next_race_no, next_race_time


def fetch_today_schedule(date_yyyymmdd):
    """
    BOAT RACE公式サイトの開催一覧ページから、24場それぞれについて
    「本日開催しているか」「開催日目」「状態（発売中/終了/未開催）」を取得する。

    サイト構造の変化に弱い可能性があるため、取得できなかった場は
    holding=False（休み扱い）にフォールバックする。アプリ側はholding=False
    の場をグレー表示にしつつ、手動選択自体は可能なままにする。

    戻り値: DataFrame（列: jcd, holding, day_label, status,
                        next_race_no, next_race_time）
    """
    out_rows = [
        {
            "jcd": code,
            "holding": False,
            "day_label": "",
            "status": "",
            "next_race_no": None,
            "next_race_time": "",
        }
        for code in VENUE_CODES
    ]
    by_code = {r["jcd"]: r for r in out_rows}

    try:
        html, url = _get(date_yyyymmdd)
    except Exception as e:
        print("[TODAY_SCHEDULE ERROR]", type(e).__name__, str(e), flush=True)
        return pd.DataFrame(out_rows)

    soup = BeautifulSoup(html, "lxml")

    # jcd=XX を含むリンクを起点に、その祖先ブロックのテキストから
    # 開催日目・状態を読み取る。会場名リンクは1場につき複数箇所に出る
    # （カード見出し用のリンクと、各レース番号用のリンク&rno=Nなど）。
    # レース番号付きのリンクは会場カードの見出しから離れた位置にある
    # ことが多く、開催日目・状態のテキストを含まないことがあるため、
    # 「rno=を含まない」＝見出しリンクらしいものを優先して使う。
    links = soup.find_all("a", href=re.compile(r"jcd=(\d{2})"))
    print(
        "[TODAY_SCHEDULE] links found=", len(links), "url=", url, flush=True,
    )

    header_links = {}
    fallback_links = {}

    for a in links:
        href = a.get("href", "")
        m = re.search(r"jcd=(\d{2})", href)
        if not m:
            continue
        code = m.group(1)
        if code not in by_code:
            continue

        if "rno=" in href:
            fallback_links.setdefault(code, a)
        else:
            header_links.setdefault(code, a)

    for code in VENUE_CODES:
        a = header_links.get(code)
        link_kind = "header"
        if a is None:
            a = fallback_links.get(code)
            link_kind = "fallback"
        if a is None:
            continue

        # リンクの祖先を数階層たどりながら、開催日目・状態のキーワードが
        # 見つかった時点のテキストブロックを採用する。見つからないまま
        # 上限に達した場合は最後に見た（最も広い）ブロックを使う。
        #
        # 祖先を登りすぎると、隣接する別会場のカードまで巻き込んで
        # テキストが混ざることがある（実際にjcd=15/19/20で、離れた
        # 位置にある別会場の開催日目・状態を誤って拾ってしまう不具合が
        # 確認された）。1会場分のカードは「出走表」リンクを1つだけ
        # 含む想定なので、それが2つ以上含まれるブロックは複数会場が
        # 混ざったものとみなして採用しない（安全のため空扱いにする）。
        block = a
        text = ""
        matched_text = ""

        for _ in range(8):
            if block.parent is None:
                break
            block = block.parent
            text = _norm(block.get_text(" ", strip=True))

            has_day = re.search(r"初日|最終日|\d{1,2}日目", text)
            has_status = re.search(
                r"最終R発売終了|本日は非開催|開催終了|発売開始前|\d{1,2}R\s*\d{1,2}:\d{2}",
                text,
            )

            if has_day or has_status:
                matched_text = text
                # あまり広く登りすぎると隣の会場のテキストまで混ざるため、
                # 300文字を超えたら深追いせずここで確定させる
                # （1会場分のカードは通常60〜100文字程度）。
                if len(text) > 300 or (has_day and has_status):
                    break

        final_text = matched_text or text
        mixed = final_text.count("出走表") > 1

        print(
            "[TODAY_SCHEDULE] block", code, VENUES_LABEL.get(code, ""),
            "link=", link_kind,
            "len=", len(final_text),
            "mixed=", mixed,
            "text=", final_text[:160],
            flush=True,
        )

        if mixed:
            # 複数会場が混ざったブロックからは日目・状態を読み取らない。
            # holdingはリンクの存在自体から確定しているのでTrueのまま。
            by_code[code].update({"holding": True})
            continue

        day_label, status, next_no, next_time = _parse_venue_block(final_text)

        by_code[code].update({
            "holding": True,
            "day_label": day_label,
            "status": status,
            "next_race_no": next_no,
            "next_race_time": next_time,
        })

    print(
        "[TODAY_SCHEDULE] holding venues=",
        sorted(c for c, r in by_code.items() if r["holding"]),
        flush=True,
    )

    return pd.DataFrame(list(by_code.values()))



def fetch_venue_deadlines(date_yyyymmdd, jcd):
    """
    選択した会場の1R〜12Rの締切予定時刻を、BOAT RACE公式出走表から
    1回のHTTPアクセスで取得する。

    戻り値:
        {1: "10:45", 2: "11:14", ..., 12: "16:20"}
    取得できない場合は空dict。
    """
    hd = re.sub(r"\D", "", str(date_yyyymmdd))
    code = str(jcd).zfill(2)
    if len(hd) != 8:
        return {}

    url = (
        "https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?hd={hd}&jcd={code}&rno=1"
    )

    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"

        soup = BeautifulSoup(r.text, "lxml")
        page_text = _norm(soup.get_text(" ", strip=True))

        pos = page_text.find("締切予定時刻")
        if pos < 0:
            print(
                "[VENUE_DEADLINES] label not found:",
                hd, code, flush=True,
            )
            return {}

        # 「締切予定時刻」の直後に1R〜12Rの時刻が並ぶ。
        # 余計な時刻を拾いにくいよう、周辺だけを対象にする。
        window = page_text[pos:pos + 700]
        times = re.findall(
            r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)",
            window,
        )

        if len(times) < 12:
            print(
                "[VENUE_DEADLINES] insufficient times:",
                hd, code, "found=", len(times), "times=", times,
                flush=True,
            )
            return {}

        return {rno: times[rno - 1] for rno in range(1, 13)}

    except Exception as e:
        print(
            "[VENUE_DEADLINES ERROR]",
            hd, code, type(e).__name__, str(e), flush=True,
        )
        return {}
