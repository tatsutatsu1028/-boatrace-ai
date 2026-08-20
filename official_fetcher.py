from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from current_meet_fetcher import fetch_current_meet
from course_stats_fetcher import fetch_course_stats_for_race, fetch_venue_course_profile
BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.5; personal-analysis-tool)"
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
    
def _racer_id(text):
    s = _norm(text)
    m = re.search(r"^\s*(\d{4})\s*/\s*[AB][12]", s)
    return m.group(1) if m else ""

def _racer_class(text):
    s = _norm(text)
    m = re.search(r"\d{4}\s*/\s*([AB][12])", s)
    return m.group(1) if m else ""

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

    rec["racer_id"] = _racer_id(row[racer_i])
    rec["racer_name"] = _racer_name(row[racer_i])
    rec["racer_class"] = _racer_class(row[racer_i])

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
    """
    BOAT RACE公式3連単オッズを復元する。

    公式PC版は、1着艇1〜6の6ブロックが横並びで、
    各ブロックは [2着艇, 3着艇, オッズ] の3列。
    2着艇は rowspan=4 で表示されるため、_expand_table() で展開して読む。
    """
    html, url = _get("odds3t", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")

    best = None
    best_score = -1

    for table in soup.find_all("table"):
        grid = _expand_table(table)
        if not grid:
            continue

        valid = 0

        for row in grid:
            if len(row) < 18:
                continue

            for g in range(6):
                base = g * 3
                first = g + 1

                second = _lane(row[base]) if base < len(row) else None
                third = _lane(row[base + 1]) if base + 1 < len(row) else None
                odd = _num(row[base + 2]) if base + 2 < len(row) else np.nan

                if (
                    second is not None
                    and third is not None
                    and len({first, second, third}) == 3
                    and pd.notna(odd)
                    and odd >= 1
                ):
                    valid += 1

        if valid > best_score:
            best_score = valid
            best = grid

    seen = {}

    if best is not None:
        for row in best:
            if len(row) < 18:
                continue

            for g in range(6):
                first = g + 1
                base = g * 3

                second = _lane(row[base])
                third = _lane(row[base + 1])
                odd = _num(row[base + 2])

                if second is None or third is None or pd.isna(odd):
                    continue

                if len({first, second, third}) != 3:
                    continue

                if odd < 1:
                    continue

                seen[f"{first}-{second}-{third}"] = float(odd)

    if len(seen) < 100:
        page_text = _norm(soup.get_text(" ", strip=True))
        pat = re.compile(
            r"(?<!\d)([1-6])\s+([1-6])\s+([1-6])\s+(\d+(?:\.\d+)?)(?!\d)"
        )

        for a, b, c, odd_s in pat.findall(page_text):
            if len({a, b, c}) != 3:
                continue

            odd = float(odd_s)

            if odd >= 1:
                seen.setdefault(f"{a}-{b}-{c}", odd)

    out = pd.DataFrame(
        [{"combo": combo, "odds": odd} for combo, odd in seen.items()]
    )

    if len(out):
        def combo_key(s):
            a, b, c = map(int, s.split("-"))
            return a * 100 + b * 10 + c

        out["_key"] = out["combo"].map(combo_key)
        out = (
            out.sort_values("_key")
            .drop(columns="_key")
            .reset_index(drop=True)
        )

    if len(out) < 100:
        out = pd.DataFrame(columns=["combo", "odds"])

    out.attrs["source"] = url
    out.attrs["count"] = len(out)

    return out

def fetch_official_race(date_yyyymmdd, jcd, rno):
    """
    公式データ高速取得版。

    racelistだけ先に取得し、その後は独立して取得できる
    今節成績・コース適性・直前情報を並列取得する。

    選手コメント機能（ピットレポート・場コメント自動取得）は
    著作権上の懸念（他サイトのコメント文をそのまま複製・表示する
    ことになるため）から廃止した。
    """
    _t_total = time.perf_counter()

    print(
        "[FETCH_TIME] START",
        "date=", date_yyyymmdd,
        "jcd=", jcd,
        "rno=", rno,
        flush=True,
    )

    # racer_id / racer_name が後続処理で必要なため、
    # racelistだけは最初に取得する。
    try:
        _t = time.perf_counter()
        base = fetch_racelist(
            date_yyyymmdd,
            jcd,
            rno,
        )
        print(
            f"[FETCH_TIME] racelist: "
            f"{time.perf_counter() - _t:.2f}s",
            flush=True,
        )
    except Exception as e:
        raise RuntimeError(
            f"racelist取得失敗: {type(e).__name__}: {e}"
        ) from e

    # ここからは相互依存がほぼないので同時取得。
    started = time.perf_counter()

    def _timed(label, func, *args):
        t0 = time.perf_counter()
        result = func(*args)
        print(
            f"[FETCH_TIME] {label}: "
            f"{time.perf_counter() - t0:.2f}s",
            flush=True,
        )
        return result

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            "current_meet": executor.submit(
                _timed,
                "current_meet",
                fetch_current_meet,
                date_yyyymmdd,
                jcd,
                rno,
            ),
            "course_stats": executor.submit(
                _timed,
                "course_stats",
                fetch_course_stats_for_race,
                base.copy(),
            ),
            "beforeinfo": executor.submit(
                _timed,
                "beforeinfo",
                fetch_beforeinfo,
                date_yyyymmdd,
                jcd,
                rno,
            ),
            "venue_course_profile": executor.submit(
                _timed,
                "venue_course_profile",
                fetch_venue_course_profile,
                jcd,
            ),
        }

        # 今節成績
        try:
            meet = futures["current_meet"].result()

            base = base.merge(
                meet,
                on="lane",
                how="left",
            )

            print(
                "[CURRENT_MEET]",
                base[
                    [
                        "lane",
                        "current_meet_avg_finish",
                        "current_meet_top2_rate",
                        "current_meet_avg_st",
                        "current_meet_races",
                    ]
                ].to_dict("records"),
                flush=True,
            )

        except Exception as e:
            print(
                "[CURRENT_MEET ERROR]",
                type(e).__name__,
                str(e),
                flush=True,
            )

            base["current_meet_avg_finish"] = np.nan
            base["current_meet_top2_rate"] = np.nan
            base["current_meet_avg_st"] = np.nan
            base["current_meet_races"] = 0

        # コース適性
        try:
            course_stats = futures["course_stats"].result()

            base = base.merge(
                course_stats,
                on="lane",
                how="left",
            )

            print(
                "[COURSE_STATS]",
                base[
                    [
                        "lane",
                        "racer_id",
                        "course_top3_rate",
                        "course_avg_st",
                        "course_start_rank",
                    ]
                ].to_dict("records"),
                flush=True,
            )

        except Exception as e:
            print(
                "[COURSE_STATS ERROR]",
                type(e).__name__,
                str(e),
                flush=True,
            )

            base["course_top3_rate"] = np.nan
            base["course_avg_st"] = np.nan
            base["course_start_rank"] = np.nan

        # beforeinfo は主要データなので、従来どおり失敗時は全体エラーにする。
        try:
            before = futures["beforeinfo"].result()
        except Exception as e:
            raise RuntimeError(
                f"beforeinfo取得失敗: {type(e).__name__}: {e}"
            ) from e

        # オリジナル展示（直線・まわり足・1周）は自動取得を廃止した
        # （公式・非公式サイトのいずれにも安定した取得元がなく、
        # 唯一見つかったソースはロボット自動アクセスを禁止していたため）。
        # app.py側の手入力欄で埋めてもらう前提で、常に空のまま返す。
        original_exhibition = pd.DataFrame({
            "lane": range(1, 7),
            "original_straight": [None] * 6,
            "original_turn": [None] * 6,
            "original_lap": [None] * 6,
            "original_exhibition_source": [""] * 6,
            "original_exhibition_url": [""] * 6,
        })

        # 場全体のコース特性（逃げ率・決まり手）も失敗しても続行
        try:
            venue_course_profile = futures["venue_course_profile"].result()
            venue_course_profile = venue_course_profile.rename(columns={"course": "lane"})
            print(
                "[OFFICIAL_FETCHER] venue course profile done:",
                "rows=", len(venue_course_profile),
                flush=True,
            )
        except Exception as e:
            print(
                "[OFFICIAL_FETCHER] venue course profile ERROR:",
                type(e).__name__,
                str(e),
                flush=True,
            )
            venue_course_profile = pd.DataFrame({
                "lane": range(1, 7),
                "venue_course_1st": [np.nan] * 6,
                "venue_course_2nd": [np.nan] * 6,
                "venue_course_3rd": [np.nan] * 6,
                "venue_course_4th": [np.nan] * 6,
                "venue_course_5th": [np.nan] * 6,
                "venue_course_6th": [np.nan] * 6,
                "venue_kimarite_nige": [np.nan] * 6,
                "venue_kimarite_makuri": [np.nan] * 6,
                "venue_kimarite_sashi": [np.nan] * 6,
                "venue_kimarite_makuri_sashi": [np.nan] * 6,
                "venue_kimarite_nuki": [np.nan] * 6,
                "venue_kimarite_megumare": [np.nan] * 6,
            })

    print(
        f"[FETCH_TIME] parallel_block: "
        f"{time.perf_counter() - started:.2f}s",
        flush=True,
    )

    try:
        out = base.merge(
            before.drop(
                columns=["source_beforeinfo"],
                errors="ignore",
            ),
            on="lane",
            how="outer",
            suffixes=("", "_before"),
        )

        out = out.merge(
            original_exhibition,
            on="lane",
            how="left",
        )

        out = out.merge(
            venue_course_profile,
            on="lane",
            how="left",
        )

        if "racer_name_beforeinfo" in out.columns:
            if "racer_name" not in out.columns:
                out["racer_name"] = (
                    out["racer_name_beforeinfo"]
                )
            else:
                miss = (
                    out["racer_name"].isna()
                    | (
                        out["racer_name"]
                        .astype(str)
                        .str.strip()
                        == ""
                    )
                )

                out.loc[
                    miss,
                    "racer_name",
                ] = out.loc[
                    miss,
                    "racer_name_beforeinfo",
                ]

        out["venue"] = VENUES[
            str(jcd).zfill(2)
        ]
        out["race_no"] = int(rno)
        out["date"] = pd.to_datetime(
            date_yyyymmdd
        ).strftime("%Y-%m-%d")

        out["source_beforeinfo"] = (
            before["source_beforeinfo"].iloc[0]
            if len(before)
            else ""
        )

        print(
            f"[FETCH_TIME] TOTAL: "
            f"{time.perf_counter() - _t_total:.2f}s",
            flush=True,
        )

        return (
            out.sort_values("lane")
            .reset_index(drop=True)
        )

    except Exception as e:
        raise RuntimeError(
            f"統合処理失敗: {type(e).__name__}: {e}"
        ) from e
