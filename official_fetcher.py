from __future__ import annotations

import re
import unicodedata
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.4; personal-analysis-tool)"
}

VENUES = {
    "01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川","06":"浜名湖",
    "07":"蒲郡","08":"常滑","09":"津","10":"三国","11":"びわこ","12":"住之江",
    "13":"尼崎","14":"鳴門","15":"丸亀","16":"児島","17":"宮島","18":"徳山",
    "19":"下関","20":"若松","21":"芦屋","22":"福岡","23":"唐津","24":"大村"
}

PREFS = (
    "北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|"
    "神奈川|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|"
    "大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|"
    "福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄"
)

def _get(path, date_yyyymmdd, jcd, rno):
    url = f"{BASE}/{path}?hd={date_yyyymmdd}&jcd={str(jcd).zfill(2)}&rno={int(rno)}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text, url

def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()

def _num(x):
    m = re.search(r"-?\d+(?:\.\d+)?", _norm(x).replace(",", ""))
    return float(m.group(0)) if m else np.nan

def _nums(x):
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", _norm(x).replace(",", ""))]

def _lane(x):
    s = _norm(x)
    return int(s) if re.fullmatch(r"[1-6]", s) else None

def _cell_text(cell):
    return _norm(cell.get_text(" ", strip=True))

def _expand_table(table):
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

def _find_table(soup, words):
    best = None
    best_score = -1
    for t in soup.find_all("table"):
        blob = _norm(t.get_text(" ", strip=True))
        score = sum(1 for w in words if w in blob)
        if score > best_score:
            best = t
            best_score = score
    return best if best_score > 0 else None

def _racer_name(text):
    s = _norm(text)
    s = re.sub(r"^\d{4}\s*/\s*[AB][12]\s*", "", s)
    s = re.split(rf"\s+(?:{PREFS})\s*/", s, maxsplit=1)[0]
    s = re.split(r"\d+歳", s, maxsplit=1)[0]
    m = re.search(r"([一-龥々ぁ-んァ-ヶー]+(?:\s+[一-龥々ぁ-んァ-ヶー]+)*)", s)
    return _norm(m.group(1)) if m else ""

def _looks_like_racer(cell):
    s = _norm(cell)
    return bool(re.search(r"\d{4}\s*/\s*[AB][12]", s)) or (
        bool(re.search(r"[一-龥々ぁ-んァ-ヶー]{2,}", s)) and ("歳" in s or "kg" in s)
    )

def _main_race_rows(table):
    rows = {}
    for row in _expand_table(table):
        if not row:
            continue
        ln = _lane(row[0])
        if ln is None:
            continue
        if not any(_looks_like_racer(c) for c in row[:12]):
            continue
        rows.setdefault(ln, row)
    return rows

def _parse_racelist_row(ln, row):
    rec = {"lane": ln}
    racer_i = next((i for i, c in enumerate(row) if _looks_like_racer(c)), None)
    if racer_i is None:
        return rec

    rec["racer_name"] = _racer_name(row[racer_i])

    def at(offset):
        i = racer_i + offset
        return row[i] if 0 <= i < len(row) else ""

    flst = _nums(at(1))
    decimals = [x for x in flst if 0 <= x < 1]
    if decimals:
        rec["avg_st"] = decimals[-1]

    nationwide = _nums(at(2))
    if nationwide and 0 <= nationwide[0] <= 10:
        rec["racer_win_rate"] = nationwide[0]

    local = _nums(at(3))
    if local and 0 <= local[0] <= 10:
        rec["local_win_rate"] = local[0]

    motor = _nums(at(4))
    if len(motor) >= 2 and 0 <= motor[1] <= 100:
        rec["motor_2ren"] = motor[1]

    boat = _nums(at(5))
    if len(boat) >= 2 and 0 <= boat[1] <= 100:
        rec["boat_2ren"] = boat[1]

    return rec

def fetch_racelist(date_yyyymmdd, jcd, rno):
    html, url = _get("racelist", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")
    table = _find_table(soup, ["ボートレーサー", "全国", "当地", "モーター", "ボート"])

    out_rows = []
    if table is not None:
        rows = _main_race_rows(table)
        for ln in range(1, 7):
            out_rows.append(_parse_racelist_row(ln, rows.get(ln, [])))
    else:
        out_rows = [{"lane": i} for i in range(1, 7)]

    out = pd.DataFrame(out_rows)
    out["source_racelist"] = url
    return out.sort_values("lane").reset_index(drop=True)

def _before_rows(table):
    rows = {}
    for row in _expand_table(table):
        if not row:
            continue
        ln = _lane(row[0])
        if ln is None:
            continue
        joined = " ".join(row[:12])
        if "kg" not in joined and not re.search(r"\b[67]\.\d{2}\b", joined):
            continue
        rows.setdefault(ln, row)
    return rows

def _parse_before_row(ln, row):
    rec = {"lane": ln}
    if not row:
        return rec

    name_i = None
    for i, c in enumerate(row[:10]):
        s = _norm(c)
        if re.search(r"[一-龥々ぁ-んァ-ヶー]{2,}", s) and "kg" not in s and "展示" not in s:
            if not re.fullmatch(r"[枠写真体重チルト]+", s):
                name_i = i
                break

    if name_i is not None:
        rec["racer_name_beforeinfo"] = _norm(row[name_i])

        for c in row[name_i+1:name_i+5]:
            s = _norm(c)
            if "kg" in s and "weight" not in rec:
                v = _num(s)
                if pd.notna(v):
                    rec["weight"] = v

        for c in row[name_i+1:name_i+6]:
            vals = _nums(c)
            if vals:
                v = vals[0]
                if 6.0 <= v <= 8.5 and "exhibition_time" not in rec:
                    rec["exhibition_time"] = v

        if "exhibition_time" in rec:
            for c in row[name_i+1:name_i+7]:
                vals = _nums(c)
                if vals:
                    v = vals[0]
                    if -0.5 <= v <= 3.0 and "kg" not in _norm(c):
                        if abs(v * 2 - round(v * 2)) < 1e-9:
                            rec["tilt"] = v
                            break

    return rec

def _parse_start_exhibition(soup):
    result = {}
    page = _norm(soup.get_text(" ", strip=True))
    if "スタート展示" not in page:
        return result
    tail = page.split("スタート展示", 1)[1]
    tail = tail.split("水面気象情報", 1)[0]

    pat = re.compile(r"(?<!\d)([1-6])\s+(?:Image\s+)?(F)?\s*\.?\s*(\d{1,2})(?!\d)")
    for lane_s, fmark, digits in pat.findall(tail):
        v = float("0." + digits.zfill(2))
        result[int(lane_s)] = -v if fmark else v
    return result

def _weather(text):
    def grab(p):
        m = re.search(p, text)
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
    table = _find_table(soup, ["ボートレーサー", "体重", "展示", "タイム", "チルト"])

    records = {}
    if table is not None:
        for ln, row in _before_rows(table).items():
            records[ln] = _parse_before_row(ln, row)

    stmap = _parse_start_exhibition(soup)
    page_text = _norm(soup.get_text(" ", strip=True))
    wx = _weather(page_text)

    rows = []
    for ln in range(1, 7):
        rec = records.get(ln, {"lane": ln})
        rec["exhibition_st"] = stmap.get(ln, np.nan)
        rows.append(rec)

    out = pd.DataFrame(rows)
    for k, v in wx.items():
        out[k] = v
    out["source_beforeinfo"] = url
    return out.sort_values("lane").reset_index(drop=True)

def fetch_odds3t(date_yyyymmdd, jcd, rno):
    html, url = _get("odds3t", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")
    text = _norm(soup.get_text(" ", strip=True))

    seen = {}
    pat = re.compile(r"(?<!\d)([1-6])\s+([1-6])\s+([1-6])\s+(\d+(?:\.\d+)?)(?!\d)")
    for a, b, c, odd in pat.findall(text):
        if len({a,b,c}) == 3:
            v = float(odd)
            if v >= 1:
                seen.setdefault(f"{a}-{b}-{c}", v)

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
            suffixes=("", "_before"),
        )

        if "racer_name_beforeinfo" in out.columns:
            if "racer_name" not in out.columns:
                out["racer_name"] = out["racer_name_beforeinfo"]
            else:
                miss = out["racer_name"].isna() | (out["racer_name"].astype(str).str.strip() == "")
                out.loc[miss, "racer_name"] = out.loc[miss, "racer_name_beforeinfo"]

        out["venue"] = VENUES[str(jcd).zfill(2)]
        out["race_no"] = int(rno)
        out["date"] = pd.to_datetime(date_yyyymmdd).strftime("%Y-%m-%d")
        out["source_beforeinfo"] = before["source_beforeinfo"].iloc[0] if len(before) else ""
        return out.sort_values("lane").reset_index(drop=True)

    except Exception as e:
        raise RuntimeError(f"統合処理失敗: {type(e).__name__}: {e}") from e
