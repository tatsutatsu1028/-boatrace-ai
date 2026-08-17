from __future__ import annotations

import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE = "https://www.boatrace.jp/owpc/pc/data/racersearch/course"

UA = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; BoatraceAIMobile/3.1; personal-analysis-tool)"
    )
}

CACHE_TTL_SECONDS = 60 * 60

_course_cache = {}
_cache_lock = Lock()

# 同じホスト(boatrace.jp)へ6艇分アクセスするため、コネクションを
# 使い回すセッションを用意する。毎回新規接続だとTCP/TLSハンドシェイクの
# 往復が艇数分積み重なり、体感速度に大きく影響していた。
_session = requests.Session()
_session.headers.update(UA)
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


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

    r = _session.get(
        url,
        timeout=15,
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


def _fetch_racer_course_stats_uncached(racer_id):
    html, url = _get_course_page(racer_id)

    soup = BeautifulSoup(html, "lxml")
    text = _norm(soup.get_text(" ", strip=True))

    top3 = _extract_six_values(text, "コース別3連対率")
    avg_st = _extract_six_values(text, "コース別平均スタートタイミング")
    start_rank = _extract_six_values(text, "コース別スタート順")

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


def fetch_racer_course_stats(racer_id):
    key = _norm(racer_id)
    now = time.time()

    with _cache_lock:
        cached = _course_cache.get(key)

        if cached is not None:
            ts, df = cached
            if now - ts < CACHE_TTL_SECONDS:
                return df.copy()

    out = _fetch_racer_course_stats_uncached(key)

    with _cache_lock:
        _course_cache[key] = (now, out.copy())

    return out


def _fetch_one_lane(lane, racer_id):
    rec = {
        "lane": int(lane),
        "course_top3_rate": np.nan,
        "course_avg_st": np.nan,
        "course_start_rank": np.nan,
    }

    racer_id = _norm(racer_id)

    if not racer_id:
        return rec

    try:
        stats = fetch_racer_course_stats(racer_id)

        hit = stats[stats["course"] == int(lane)]

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

    return rec


def fetch_course_stats_for_race(race):
    jobs = []

    for _, row in race.iterrows():
        jobs.append((
            int(row["lane"]),
            _norm(row.get("racer_id", "")),
        ))

    rows = []
    # 1レース最大6艇なので、全艇を1バッチで同時に取得する。
    # 従来は上限4だったため6艇だと2バッチ（待ち時間が約2倍）になっていた。
    max_workers = min(6, max(1, len(jobs)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_fetch_one_lane, lane, racer_id): lane
            for lane, racer_id in jobs
        }

        for future in as_completed(future_map):
            lane = future_map[future]

            try:
                rows.append(future.result())
            except Exception as e:
                print(
                    "[COURSE_STATS WORKER ERROR]",
                    "lane=", lane,
                    type(e).__name__,
                    str(e),
                )
                rows.append({
                    "lane": int(lane),
                    "course_top3_rate": np.nan,
                    "course_avg_st": np.nan,
                    "course_start_rank": np.nan,
                })

    out = (
        pd.DataFrame(rows)
        .sort_values("lane")
        .reset_index(drop=True)
    )

    print(
        "[COURSE_STATS FAST]",
        out.to_dict("records"),
    )

    return out
