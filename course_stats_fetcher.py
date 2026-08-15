from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE = "https://www.boatrace.jp/owpc/pc/data/racersearch/course"

UA = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; BoatraceAIMobile/3.0; personal-analysis-tool)"
    )
}


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _num(x):
    s = _norm(x).replace("%", "")

    if not s or s in {"-", "--"}:
        return np.nan

    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan


def _get_course_page(racer_id):
    url = f"{BASE}?toban={str(racer_id).strip()}"

    r = requests.get(
        url,
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    return r.text, url


def _extract_six_values(text, heading):
    pos = text.find(heading)

    if pos < 0:
        return {}

    tail = text[pos + len(heading):]

    next_heads = (
        "コース別進入率",
        "コース別3連対率",
        "コース別平均スタートタイミング",
        "コース別スタート順",
    )

    ends = []

    for h in next_heads:
        p = tail.find(h)
        if p >= 0:
            ends.append(p)

    if ends:
        tail = tail[:min(ends)]

    out = {}

    pat = re.compile(
        r"(?:^|\s)([1-6])\s+(-{1,2}|\d+(?:\.\d+)?%?)"
    )

    for lane_s, value_s in pat.findall(tail):
        lane = int(lane_s)

        if lane not in out:
            out[lane] = _num(value_s)

    return out


def fetch_racer_course_stats(racer_id):
    html, url = _get_course_page(racer_id)

    soup = BeautifulSoup(html, "lxml")
    text = _norm(soup.get_text(" ", strip=True))

    top3 = _extract_six_values(
        text,
        "コース別3連対率",
    )

    avg_st = _extract_six_values(
        text,
        "コース別平均スタートタイミング",
    )

    start_rank = _extract_six_values(
        text,
        "コース別スタート順",
    )

    rows = []

    for course in range(1, 7):
        rows.append({
            "course": course,
            "course_top3_rate": top3.get(course, np.nan),
            "course_avg_st": avg_st.get(course, np.nan),
            "course_start_rank": start_rank.get(course, np.nan),
        })

    out = pd.DataFrame(rows)
    out.attrs["source"] = url

    return out


def fetch_course_stats_for_race(race):
    rows = []

    for _, row in race.iterrows():
        lane = int(row["lane"])
        racer_id = _norm(row.get("racer_id", ""))

        rec = {
            "lane": lane,
            "course_top3_rate": np.nan,
            "course_avg_st": np.nan,
            "course_start_rank": np.nan,
        }

        if not racer_id:
            rows.append(rec)
            continue

        try:
            stats = fetch_racer_course_stats(racer_id)

            hit = stats[stats["course"] == lane]

            if len(hit):
                x = hit.iloc[0]
                rec["course_top3_rate"] = x["course_top3_rate"]
                rec["course_avg_st"] = x["course_avg_st"]
                rec["course_start_rank"] = x["course_start_rank"]

        except Exception as e:
            print(
                "[COURSE_STATS ERROR]",
                "lane=", lane,
                "racer_id=", racer_id,
                type(e).__name__,
                str(e),
            )

        rows.append(rec)

    return (
        pd.DataFrame(rows)
        .sort_values("lane")
        .reset_index(drop=True)
    )
