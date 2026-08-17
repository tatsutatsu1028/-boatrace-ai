from __future__ import annotations

import re
import unicodedata

import pandas as pd
from bs4 import BeautifulSoup

from venue_comments import (
    COMMON_CMS_VENUES,
    VENUE_SOURCE_LABELS,
    _get,
    _looks_like_no_data_page,
    _norm,
)

# オリジナル展示（直線・まわり足・1周）が載っていそうなページ候補。
# 「yosou-cyokuzen」（直前情報・予想）が本命だが、場によって
# ページ名が違う可能性があるため複数試す。
# 開催中でないと実データでの検証ができないため、初回は
# デバッグログを多めに仕込んでおき、開催中に微調整する前提。
ORIGINAL_EXHIBITION_PAGE_CANDIDATES = (
    "index.php?page=yosou-cyokuzen&race={rno}",
    "index.php?page=yosou-cyokuzen",
    "index.php?page=raceinfo-tenbo&race={rno}",
)

# 表のヘッダーに含まれていそうなキーワード（列の意味を判定するため）
_STRAIGHT_KEYS = ("直線",)
_TURN_KEYS = ("まわり足", "回り足", "廻り足")
_LAP_KEYS = ("1周", "一周", "周回")


def _to_float(s):
    s = _norm(s)
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _empty(race):
    out = race[["lane"]].copy()
    out["original_straight"] = None
    out["original_turn"] = None
    out["original_lap"] = None
    out["original_exhibition_source"] = ""
    out["original_exhibition_url"] = ""
    return out


def _find_exhibition_table(soup):
    """
    直線・まわり足・1周のいずれかのキーワードをヘッダーに含む
    テーブルを探す。見つかった場合、(table, col_map) を返す。
    col_map は {列インデックス: "original_straight" 等} のdict。
    """
    for table in soup.find_all("table"):
        header_cells = table.find_all(["th"])
        if not header_cells:
            # theadが無い場合、先頭行をヘッダー扱いにする
            first_tr = table.find("tr")
            header_cells = first_tr.find_all(["td", "th"]) if first_tr else []

        col_map = {}
        for i, cell in enumerate(header_cells):
            text = _norm(cell.get_text(" ", strip=True))
            if any(k in text for k in _STRAIGHT_KEYS):
                col_map[i] = "original_straight"
            elif any(k in text for k in _TURN_KEYS):
                col_map[i] = "original_turn"
            elif any(k in text for k in _LAP_KEYS):
                col_map[i] = "original_lap"

        if col_map:
            return table, col_map

    return None, {}


def _parse_exhibition_table(table, col_map):
    """艇番(1-6)をキーに {lane: {"original_straight":..,...}} を返す。"""
    out = {}

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        row_text = [_norm(c.get_text(" ", strip=True)) for c in cells]

        # 先頭付近のセルから艇番(1-6の単独数字)を探す
        lane = None
        for c in row_text[:2]:
            if re.fullmatch(r"[1-6]", c):
                lane = int(c)
                break

        if lane is None:
            continue

        rec = out.setdefault(lane, {})
        for idx, key in col_map.items():
            if idx < len(row_text):
                v = _to_float(row_text[idx])
                if v is not None:
                    rec[key] = v

    return out


def fetch_original_exhibition(date_yyyymmdd, jcd, rno, race):
    code = str(jcd).zfill(2)
    out = _empty(race)

    base = COMMON_CMS_VENUES.get(code)
    if not base:
        return out

    label = VENUE_SOURCE_LABELS.get(code, f"{code}公式")

    for page_tmpl in ORIGINAL_EXHIBITION_PAGE_CANDIDATES:
        url = f"{base}/sp/{page_tmpl.format(rno=int(rno))}"

        try:
            html = _get(url)
        except Exception as e:
            print(f"[ORIG_EXPO {code} ERROR]", url, type(e).__name__, str(e))
            continue

        if _looks_like_no_data_page(html):
            print(f"[ORIG_EXPO {code}] no data page:", url)
            continue

        soup = BeautifulSoup(html, "lxml")
        table, col_map = _find_exhibition_table(soup)

        if table is None:
            # デバッグ: ページは取れたがそれらしいテーブルが無かった場合、
            # 開催中に構造を特定できるよう、テーブル一覧を軽くログに出す。
            table_count = len(soup.find_all("table"))
            print(
                f"[ORIG_EXPO {code}] exhibition table not found. "
                f"url={url} tables_on_page={table_count}"
            )
            continue

        parsed = _parse_exhibition_table(table, col_map)
        print(f"[ORIG_EXPO {code}] parsed lanes=", list(parsed.keys()), "url=", url)

        for idx, row in race.iterrows():
            lane = int(row["lane"])
            rec = parsed.get(lane)
            if not rec:
                continue

            for key in ("original_straight", "original_turn", "original_lap"):
                if key in rec:
                    out.loc[idx, key] = rec[key]

            out.loc[idx, "original_exhibition_source"] = f"{label}・オリジナル展示"
            out.loc[idx, "original_exhibition_url"] = url

        got_any = out[["original_straight", "original_turn", "original_lap"]].notna().any().any()
        if got_any:
            break

    return out
