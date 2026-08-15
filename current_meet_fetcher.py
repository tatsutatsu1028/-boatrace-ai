from __future__ import annotations

import re
import unicodedata
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/3.0; personal-analysis-tool)"
}


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(date_yyyymmdd, jcd, rno):
    url = (
        f"{BASE}/racelist?"
        f"hd={date_yyyymmdd}&jcd={str(jcd).zfill(2)}&rno={int(rno)}"
    )
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text, url


def _cell_text(cell):
    return _norm(cell.get_text(" ", strip=True))


def _expand_table(table):
    """
    rowspan / colspan を展開して、見た目どおりの2次元グリッドにする。
    BOAT RACE出走表の「今節成績」は1艇あたり複数行に分かれているため必要。
    """
    grid = []
    pending = {}

    for tr in table.find_all("tr"):
        row = []
        col = 0

        def consume():
            nonlocal col
            while col in pending:
                remain, text = pending[col]
                row.append(text)
                if remain <= 1:
                    del pending[col]
                else:
                    pending[col] = [remain - 1, text]
                col += 1

        consume()

        for cell in tr.find_all(["th", "td"], recursive=False):
            consume()
            text = _cell_text(cell)

            try:
                rowspan = max(1, int(cell.get("rowspan", 1)))
            except Exception:
                rowspan = 1

            try:
                colspan = max(1, int(cell.get("colspan", 1)))
            except Exception:
                colspan = 1

            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[col] = [rowspan - 1, text]
                col += 1

        consume()
        grid.append(row)

    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def _find_racelist_table(soup):
    best = None
    best_score = -1

    for table in soup.find_all("table"):
        blob = _norm(table.get_text(" ", strip=True))
        score = sum(
            1
            for w in (
                "ボートレーサー",
                "全国",
                "当地",
                "モーター",
                "ボート",
                "平均ST",
            )
            if w in blob
        )
        if score > best_score:
            best = table
            best_score = score

    return best if best_score >= 4 else None


def _lane(x):
    s = _norm(x)
    return int(s) if re.fullmatch(r"[1-6]", s) else None


def _looks_like_racer(cell):
    s = _norm(cell)
    return bool(re.search(r"\d{4}\s*/\s*[AB][12]", s))


def _parse_st(x):
    """
    .14 -> 0.14
    F.03 -> -0.03
    0.14 -> 0.14
    """
    s = _norm(x).replace(" ", "")

    if not s:
        return np.nan

    f = s.startswith("F")
    s = s.lstrip("F")

    m = re.fullmatch(r"\.?(\d{1,2})", s)
    if m:
        v = float("0." + m.group(1).zfill(2))
        return -v if f else v

    m = re.fullmatch(r"0\.(\d{1,2})", s)
    if m:
        v = float(s)
        return -v if f else v

    return np.nan


def _parse_finish(x):
    """
    通常の1〜6着だけ数値として採用。
    F/L/失格/欠場などは平均着順・2連対率の母数から除外する。
    """
    s = _norm(x)
    if re.fullmatch(r"[1-6]", s):
        return float(s)
    return np.nan


def _parse_course(x):
    s = _norm(x)
    if re.fullmatch(r"[1-6]", s):
        return int(s)
    return None


def _lane_row_groups(grid):
    """
    rowspan展開後の表を艇番ごとにまとめる。
    通常は各艇について
      1行目: レースNo
      2行目: 進入コース
      3行目: ST
      4行目: 成績
    の4行が得られる。
    """
    groups = {i: [] for i in range(1, 7)}

    for row in grid:
        if not row:
            continue

        ln = _lane(row[0])
        if ln is None:
            continue

        racer_i = next(
            (i for i, cell in enumerate(row) if _looks_like_racer(cell)),
            None,
        )
        if racer_i is None:
            continue

        groups[ln].append((row, racer_i))

    return groups


def _parse_lane_meet(rows, lane):
    rec = {
        "lane": int(lane),
        "current_meet_avg_finish": np.nan,
        "current_meet_top2_rate": np.nan,
        "current_meet_avg_st": np.nan,
        "current_meet_races": 0,
    }

    if len(rows) < 4:
        return rec

    # 同一艇4行のうち、先頭4行を今節成績の
    # raceNo / course / ST / finish として扱う。
    # BOAT RACE公式PC版の現在の出走表レイアウトに対応。
    main_row, racer_i = rows[0]
    course_row, course_racer_i = rows[1]
    st_row, st_racer_i = rows[2]
    finish_row, finish_racer_i = rows[3]

    # 基礎データは
    # racer / F・L・平均ST / 全国 / 当地 / motor / boat
    # まで6セル分あるため、その後ろが今節成績ブロック。
    starts = [
        racer_i + 6,
        course_racer_i + 6,
        st_racer_i + 6,
        finish_racer_i + 6,
    ]
    start = max(starts)

    width = min(
        len(main_row),
        len(course_row),
        len(st_row),
        len(finish_row),
    )

    sts = []
    finishes = []

    # 「早見」等の末尾セルを誤採用しないよう、
    # course/ST/finish の3行が同じ位置で有効な場合だけ採用。
    for col in range(start, width):
        course = _parse_course(course_row[col])
        st = _parse_st(st_row[col])
        finish = _parse_finish(finish_row[col])

        if course is None or pd.isna(st):
            continue

        sts.append(float(st))
        if pd.notna(finish):
            finishes.append(float(finish))

    rec["current_meet_races"] = len(sts)

    if sts:
        rec["current_meet_avg_st"] = float(np.mean(sts))

    if finishes:
        rec["current_meet_avg_finish"] = float(np.mean(finishes))
        rec["current_meet_top2_rate"] = float(
            np.mean([f <= 2 for f in finishes]) * 100.0
        )

    return rec


def fetch_current_meet(date_yyyymmdd, jcd, rno):
    """
    BOAT RACE公式の出走表から「今節成績」を6艇分取得。

    返す列:
      lane
      current_meet_avg_finish  今節平均着順
      current_meet_top2_rate   今節2連対率(%)
      current_meet_avg_st      今節平均ST
      current_meet_races       今節出走数

    初日は過去走が無いため races=0、他3項目は NaN になる。
    """
    html, url = _get(date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")

    table = _find_racelist_table(soup)
    if table is None:
        out = pd.DataFrame(
            {
                "lane": range(1, 7),
                "current_meet_avg_finish": [np.nan] * 6,
                "current_meet_top2_rate": [np.nan] * 6,
                "current_meet_avg_st": [np.nan] * 6,
                "current_meet_races": [0] * 6,
            }
        )
        out.attrs["source"] = url
        return out

    grid = _expand_table(table)
    groups = _lane_row_groups(grid)

    rows = [
        _parse_lane_meet(groups.get(lane, []), lane)
        for lane in range(1, 7)
    ]

    out = pd.DataFrame(rows)
    out.attrs["source"] = url
    return out.sort_values("lane").reset_index(drop=True)
