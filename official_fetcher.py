from __future__ import annotations
import re
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.2; personal-analysis-tool)"
}

VENUES = {
    "01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川","06":"浜名湖",
    "07":"蒲郡","08":"常滑","09":"津","10":"三国","11":"びわこ","12":"住之江",
    "13":"尼崎","14":"鳴門","15":"丸亀","16":"児島","17":"宮島","18":"徳山",
    "19":"下関","20":"若松","21":"芦屋","22":"福岡","23":"唐津","24":"大村"
}

def _get(path, date_yyyymmdd, jcd, rno):
    url = f"{BASE}/{path}?hd={date_yyyymmdd}&jcd={str(jcd).zfill(2)}&rno={int(rno)}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text, url

def _clean_text(x):
    if x is None:
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()

def _to_float(x):
    s = _clean_text(x).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan

def _html_tables(html):
    """Parse HTML tables with BeautifulSoup only. Never uses pandas.read_html."""
    soup = BeautifulSoup(html, "lxml")
    tables = []

    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            rows.append([_clean_text(c.get_text(" ", strip=True)) for c in cells])

        if not rows:
            continue

        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        # Give every column a guaranteed string name.
        columns = [f"col_{i}" for i in range(width)]
        df = pd.DataFrame(rows, columns=columns)
        tables.append(df)

    return tables

def _table_blob(df):
    vals = np.asarray(df.astype(str).values, dtype=object).ravel()
    return " ".join("" if x is None else str(x) for x in vals)

def _best_table(html, keywords, min_rows=6):
    best = None
    best_score = -1
    for t in _html_tables(html):
        blob = _table_blob(t)
        score = sum(1 for k in keywords if k in blob)
        if len(t) >= min_rows and score > best_score:
            best = t
            best_score = score
    if best_score <= 0:
        return None
    return best

def _extract_weather(text):
    def grab(pattern):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else np.nan
    return {
        "temperature": grab(r"気温\s*([0-9.]+)"),
        "wind_speed": grab(r"風速\s*([0-9.]+)"),
        "water_temperature": grab(r"水温\s*([0-9.]+)"),
        "wave_height": grab(r"波高\s*([0-9.]+)")
    }

def _extract_lane_rows_from_text(table):
    """
    Convert a generic HTML table into six boat rows using the first occurrence
    of 1..6 as lane anchors. Keeps each row as joined text for heuristic parsing.
    """
    lane_rows = {}
    for _, row in table.iterrows():
        cells = [_clean_text(x) for x in row.tolist()]
        for cell in cells:
            if re.fullmatch(r"[1-6]", cell):
                lane = int(cell)
                lane_rows.setdefault(lane, " ".join(cells))
                break
    return lane_rows

def fetch_beforeinfo(date_yyyymmdd, jcd, rno):
    html, url = _get("beforeinfo", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")
    page_text = _clean_text(soup.get_text(" ", strip=True))

    out = pd.DataFrame({"lane": range(1, 7)})
    target = _best_table(html, ["展示", "タイム", "体重", "チルト"], min_rows=6)

    if target is not None:
        lane_rows = _extract_lane_rows_from_text(target)
        records = []

        for lane in range(1, 7):
            txt = lane_rows.get(lane, "")
            rec = {"lane": lane}

            # Exhibition time around 6.xx / 7.xx sec
            times = [float(x) for x in re.findall(r"\b([67]\.\d{2})\b", txt)]
            if times:
                rec["exhibition_time"] = times[-1]

            # Weight around 40-60kg
            weights = [float(x) for x in re.findall(r"\b(4\d(?:\.\d)?|5\d(?:\.\d)?|6\d(?:\.\d)?)\b", txt)]
            if weights:
                rec["weight"] = weights[0]

            # Tilt values such as -0.5, 0.0, 0.5, 1.0, 3.0
            tilts = [float(x) for x in re.findall(r"(?<!\d)(-?0\.5|-?0\.0|0|0\.5|1\.0|1\.5|2\.0|2\.5|3\.0)(?!\d)", txt)]
            if tilts:
                rec["tilt"] = tilts[-1]

            records.append(rec)

        temp = pd.DataFrame(records)
        out = out.merge(temp, on="lane", how="left")

    weather = _extract_weather(page_text)
    for k, v in weather.items():
        out[k] = v

    # Start exhibition ST: extract from visible text near "スタート展示".
    out["exhibition_st"] = np.nan
    if "スタート展示" in page_text:
        tail = page_text.split("スタート展示", 1)[1]
        raw_st = re.findall(r"\bF?\s*\.?\d{1,2}\b", tail)
        parsed = []
        for token in raw_st:
            t = token.replace(" ", "")
            if t.startswith("F"):
                d = re.sub(r"\D", "", t)
                parsed.append(-float("0." + d) if d else np.nan)
            else:
                d = re.sub(r"\D", "", t)
                if d and len(d) <= 2:
                    parsed.append(float("0." + d))
            if len(parsed) == 6:
                break
        if len(parsed) == 6:
            out["exhibition_st"] = parsed

    out["source_beforeinfo"] = url
    return out

def fetch_racelist(date_yyyymmdd, jcd, rno):
    html, url = _get("racelist", date_yyyymmdd, jcd, rno)
    target = _best_table(html, ["レーサー", "全国", "当地", "モーター", "ボート", "勝率"], min_rows=6)

    out = pd.DataFrame({"lane": range(1, 7)})
    if target is None:
        out["source_racelist"] = url
        return out

    lane_rows = _extract_lane_rows_from_text(target)
    records = []

    for lane in range(1, 7):
        txt = lane_rows.get(lane, "")
        rec = {"lane": lane}

        # Name: prefer Japanese text token after lane; best effort.
        name_candidates = re.findall(r"[一-龥ぁ-んァ-ヶー]{2,12}", txt)
        blacklist = {"全国", "当地", "モーター", "ボート", "勝率", "二連率", "三連率", "平均"}
        name_candidates = [x for x in name_candidates if x not in blacklist]
        if name_candidates:
            rec["racer_name"] = name_candidates[0]

        # All decimal numbers in plausible win-rate range.
        nums = [float(x) for x in re.findall(r"\b(\d{1,2}\.\d{1,2})\b", txt)]

        # Heuristic assignments. UI remains editable.
        winrates = [x for x in nums if 1.0 <= x <= 9.99]
        percentages = [x for x in nums if 10.0 <= x <= 99.99]

        if len(winrates) >= 1:
            rec["racer_win_rate"] = winrates[0]
        if len(winrates) >= 2:
            rec["local_win_rate"] = winrates[1]
        if len(percentages) >= 1:
            rec["motor_2ren"] = percentages[-2] if len(percentages) >= 2 else percentages[0]
        if len(percentages) >= 2:
            rec["boat_2ren"] = percentages[-1]

        # Average ST usually .xx
        stm = re.search(r"(?<!\d)0?\.(\d{2})(?!\d)", txt)
        if stm:
            rec["avg_st"] = float("0." + stm.group(1))

        records.append(rec)

    temp = pd.DataFrame(records)
    out = out.merge(temp, on="lane", how="left")
    out["source_racelist"] = url
    return out

def fetch_odds3t(date_yyyymmdd, jcd, rno):
    html, url = _get("odds3t", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")

    # Parse text stream without pandas.
    text = _clean_text(soup.get_text(" ", strip=True))

    # Try direct patterns like 1 2 3 12.5
    pattern = re.compile(
        r"(?<!\d)([1-6])\s+([1-6])\s+([1-6])\s+(\d+(?:\.\d+)?)"
    )
    seen = {}
    for a, b, c, odd in pattern.findall(text):
        if len({a, b, c}) != 3:
            continue
        val = float(odd)
        if val < 1:
            continue
        seen.setdefault(f"{a}-{b}-{c}", val)

    # Fallback: scan each table cell stream.
    if len(seen) < 60:
        for t in _html_tables(html):
            vals = [_clean_text(x) for x in np.asarray(t.values, dtype=object).ravel()]
            tokens = []
            for x in vals:
                if re.fullmatch(r"[1-6]", x):
                    tokens.append(("boat", int(x)))
                elif re.fullmatch(r"\d+(?:\.\d+)?", x):
                    tokens.append(("num", float(x)))

            for i in range(len(tokens) - 3):
                a, b, c, d = tokens[i:i+4]
                if a[0] == b[0] == c[0] == "boat" and d[0] == "num":
                    boats = (a[1], b[1], c[1])
                    if len(set(boats)) == 3 and d[1] >= 1:
                        seen.setdefault(f"{boats[0]}-{boats[1]}-{boats[2]}", d[1])

    out = pd.DataFrame([{"combo": k, "odds": v} for k, v in seen.items()])
    if len(out) < 60:
        out = pd.DataFrame(columns=["combo", "odds"])
    out.attrs["source"] = url
    return out

def fetch_official_race(date_yyyymmdd, jcd, rno):
    try:
        base = fetch_racelist(date_yyyymmdd, jcd, rno)
    except Exception as e:
        raise RuntimeError(f"racelist取得失敗: {type(e).__name__}: {e}") from e

    try:
        before = fetch_beforeinfo(date_yyyymmdd, jcd, rno)
    except Exception as e:
        raise RuntimeError(f"beforeinfo取得失敗: {type(e).__name__}: {e}") from e

    try:
        out = base.merge(
            before.drop(columns=["source_beforeinfo"], errors="ignore"),
            on="lane",
            how="outer",
        )
        out["venue"] = VENUES[str(jcd).zfill(2)]
        out["race_no"] = int(rno)
        out["date"] = pd.to_datetime(date_yyyymmdd).strftime("%Y-%m-%d")
        out["source_beforeinfo"] = before["source_beforeinfo"].iloc[0] if len(before) else ""
        return out.sort_values("lane").reset_index(drop=True)
    except Exception as e:
        raise RuntimeError(f"統合処理失敗: {type(e).__name__}: {e}") from e
