from __future__ import annotations

import re
import unicodedata
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.3; personal-analysis-tool)"
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

def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _num(x):
    s = _norm(x).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan

def _cells(tr):
    return [_norm(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]

def _lane(x):
    s = _norm(x)
    return int(s) if re.fullmatch(r"[1-6]", s) else None

def _split_numbers(cell):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", _norm(cell))]

def _find_table(soup, required_words):
    best = None
    best_score = -1
    for table in soup.find_all("table"):
        blob = _norm(table.get_text(" ", strip=True))
        score = sum(1 for w in required_words if w in blob)
        if score > best_score:
            best = table
            best_score = score
    return best if best_score > 0 else None

def _extract_racer_name(racer_cell):
    text = _norm(racer_cell)
    # Remove registration no/class and branch/origin + age/weight.
    text = re.sub(r"^\d{4}\s*/\s*[AB][12]\s*", "", text)
    # Name comes before prefecture/origin or age section.
    text = re.split(r"\s+(?:北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)\b", text, maxsplit=1)[0]
    text = re.split(r"\d+歳", text, maxsplit=1)[0]
    # Keep Japanese name-like text, normalize internal spacing.
    m = re.search(r"([一-龥々ぁ-んァ-ヶー]+(?:\s+[一-龥々ぁ-んァ-ヶー]+)*)", text)
    return _norm(m.group(1)) if m else ""

def _parse_racelist_rows(table):
    records = {}
    if table is None:
        return records

    for tr in table.find_all("tr"):
        cells = _cells(tr)
        if not cells:
            continue

        # The official row starts with the lane. Do not search other cells.
        ln = _lane(cells[0])
        if ln is None:
            continue

        # Official current layout:
        # 0 lane / 1 photo / 2 racer / 3 F,L,avgST /
        # 4 nationwide / 5 local / 6 motor / 7 boat / ...
        rec = {"lane": ln}

        if len(cells) >= 3:
            rec["racer_name"] = _extract_racer_name(cells[2])

        if len(cells) >= 4:
            nums = _split_numbers(cells[3])
            # "F1 L0 0.13" -> last decimal is average ST
            decimals = [x for x in nums if 0 <= x < 1]
            if decimals:
                rec["avg_st"] = decimals[-1]

        if len(cells) >= 5:
            nums = _split_numbers(cells[4])
            if nums:
                rec["racer_win_rate"] = nums[0]

        if len(cells) >= 6:
            nums = _split_numbers(cells[5])
            if nums:
                rec["local_win_rate"] = nums[0]

        if len(cells) >= 7:
            nums = _split_numbers(cells[6])
            # No / 2連率 / 3連率
            if len(nums) >= 2:
                rec["motor_2ren"] = nums[1]

        if len(cells) >= 8:
            nums = _split_numbers(cells[7])
            if len(nums) >= 2:
                rec["boat_2ren"] = nums[1]

        records[ln] = rec

    return records

def fetch_racelist(date_yyyymmdd, jcd, rno):
    html, url = _get("racelist", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")

    table = _find_table(soup, ["ボートレーサー", "全国", "当地", "モーター", "ボート"])
    records = _parse_racelist_rows(table)

    out = pd.DataFrame([records.get(i, {"lane": i}) for i in range(1, 7)])
    out["source_racelist"] = url
    return out.sort_values("lane").reset_index(drop=True)

def _parse_beforeinfo_rows(table):
    records = {}
    if table is None:
        return records

    for tr in table.find_all("tr"):
        cells = _cells(tr)
        if not cells:
            continue

        ln = _lane(cells[0])
        if ln is None:
            continue

        # Official current layout:
        # 0 lane / 1 photo / 2 racer / 3 weight /
        # 4 exhibition time / 5 tilt / 6 prop / 7 parts / 8 prev result
        rec = {"lane": ln}

        if len(cells) >= 3:
            rec["racer_name_beforeinfo"] = _norm(cells[2])

        if len(cells) >= 4:
            rec["weight"] = _num(cells[3])

        if len(cells) >= 5:
            val = _num(cells[4])
            if pd.notna(val) and 6.0 <= val <= 8.5:
                rec["exhibition_time"] = val

        if len(cells) >= 6:
            rec["tilt"] = _num(cells[5])

        records[ln] = rec

    return records

def _parse_start_exhibition(soup):
    result = {}
    for table in soup.find_all("table"):
        blob = _norm(table.get_text(" ", strip=True))
        if "コース" not in blob or "ST" not in blob:
            continue

        for tr in table.find_all("tr"):
            cells = _cells(tr)
            if not cells:
                continue

            # Search the first cell only for course/lane.
            ln = _lane(cells[0])
            if ln is None:
                continue

            rowtext = " ".join(cells)
            m = re.search(r"(F)?\s*\.?\s*(\d{1,2})(?!\d)", rowtext)
            if m:
                v = float("0." + m.group(2).zfill(2))
                result[ln] = -v if m.group(1) else v
        if result:
            break

    return result

def _weather(page_text):
    def grab(pattern):
        m = re.search(pattern, page_text)
        return float(m.group(1)) if m else np.nan

    return {
        "temperature": grab(r"気温\s*([0-9.]+)"),
        "wind_speed": grab(r"風速\s*([0-9.]+)"),
        "water_temperature": grab(r"水温\s*([0-9.]+)"),
        "wave_height": grab(r"波高\s*([0-9.]+)")
    }

def fetch_beforeinfo(date_yyyymmdd, jcd, rno):
    html, url = _get("beforeinfo", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")
    page_text = _norm(soup.get_text(" ", strip=True))

    table = _find_table(soup, ["ボートレーサー", "体重", "展示", "タイム", "チルト"])
    records = _parse_beforeinfo_rows(table)
    stmap = _parse_start_exhibition(soup)

    rows = []
    for i in range(1, 7):
        rec = records.get(i, {"lane": i})
        rec["exhibition_st"] = stmap.get(i, np.nan)
        rows.append(rec)

    out = pd.DataFrame(rows)

    wx = _weather(page_text)
    for k, v in wx.items():
        out[k] = v

    out["source_beforeinfo"] = url
    return out.sort_values("lane").reset_index(drop=True)

def fetch_odds3t(date_yyyymmdd, jcd, rno):
    html, url = _get("odds3t", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")

    seen = {}

    # Parse each table row, keeping boat numbers only where cells are exact 1..6.
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = _cells(tr)
            if not cells:
                continue

            # Some official odds layouts put multiple triplets in one row.
            # Use a conservative sliding scan over exact cell tokens.
            for i in range(max(0, len(cells) - 3)):
                a = _lane(cells[i])
                b = _lane(cells[i+1]) if i+1 < len(cells) else None
                c = _lane(cells[i+2]) if i+2 < len(cells) else None
                odd = _num(cells[i+3]) if i+3 < len(cells) else np.nan

                if a and b and c and len({a,b,c}) == 3 and pd.notna(odd) and odd >= 1:
                    seen.setdefault(f"{a}-{b}-{c}", float(odd))

    out = pd.DataFrame([{"combo": k, "odds": v} for k, v in seen.items()])
    if len(out) < 60:
        # Return empty rather than trusting a partial/misaligned parse.
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
            suffixes=("", "_before")
        )

        # Racelist name first; if absent, use beforeinfo name.
        if "racer_name_beforeinfo" in out.columns:
            if "racer_name" not in out.columns:
                out["racer_name"] = out["racer_name_beforeinfo"]
            else:
                missing = out["racer_name"].isna() | (out["racer_name"].astype(str).str.strip() == "")
                out.loc[missing, "racer_name"] = out.loc[missing, "racer_name_beforeinfo"]

        out["venue"] = VENUES[str(jcd).zfill(2)]
        out["race_no"] = int(rno)
        out["date"] = pd.to_datetime(date_yyyymmdd).strftime("%Y-%m-%d")
        out["source_beforeinfo"] = before["source_beforeinfo"].iloc[0] if len(before) else ""

        return out.sort_values("lane").reset_index(drop=True)

    except Exception as e:
        raise RuntimeError(f"統合処理失敗: {type(e).__name__}: {e}") from e
