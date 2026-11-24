"""
会場が配っている出走表PDF（印刷用）から、朝の予想（展示前モデル）に使う番組表・出走表の値を読み取る。

公式サイト（boatrace.jp）がプログラムからの取得を断るようになった（2026-11-18〜）ため、
利用者が手で集めた出走表PDFから朝の予想を作るための部品。自動でPDFを集めることはしない。

対応している形: 常滑（jcd=08）の出走表PDF（1ページに6レース・1艇2行）。
他の会場はPDFの作りが違う可能性があるので、読めなかったレースは返さない（6艇そろわないレースは捨てる）。

返すもの（parse_pdf）:
  programs  : official_download.parse_b と同じ列（番組表）。当地勝率はPDFに無いので空
  racelists : race_key・lane と ml_pre.RACELIST_COLS（出走表ページの値）。モーター・ボートの3連率はPDFに無いので空
  meet_runs : 今節のこれまでの走（着順・コース・ST）。PDFの「今節成績」欄から
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

KANJI_COURSE = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_NUM1 = re.compile(r"\d{1,3}\.\d|－|-")


def _z2h(s):
    return unicodedata.normalize("NFKC", str(s))


def _words(page):
    return page.extract_words(extra_attrs=["size"])


def _line(ws, y, x0, x1, tol=3.5):
    """y 付近・x0〜x1 の語を左から順に。"""
    out = [w for w in ws if abs(w["top"] - y) <= tol and x0 <= w["x0"] < x1]
    return sorted(out, key=lambda w: w["x0"])


def _text(ws, y, x0, x1, tol=3.5):
    return "".join(w["text"] for w in _line(ws, y, x0, x1, tol))


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return np.nan


def _three(s):
    """"53.958.252.6" → [53.9, 58.2, 52.6]（"－" は空）。"""
    vals = [np.nan if v in ("－", "-") else float(v) for v in _NUM1.findall(_z2h(s).replace("－", " － "))]
    vals = [v for v in vals]
    return (vals + [np.nan] * 3)[:3]


def _rate_line(s):
    """勝率（小数2桁）と2連率・3連率の3つ（小数1桁）。語がくっついていても分ける。
    "6.7642.050.068.1" → (6.76, [42.0, 50.0, 68.1])、"7.15 48.1 45.8 －" → (7.15, [48.1, 45.8, nan])。"""
    s = _z2h(s).replace(" ", "")
    m = re.match(r"(\d\.\d{2})(.*)", s)
    if not m:
        return np.nan, [np.nan] * 3
    return float(m.group(1)), _three(m.group(2))


def _race_headers(ws):
    """[(y, race_no, deadline "HH:MM", meet_title)]。"""
    heads = []
    for w in ws:
        t = _z2h(w["text"])
        if w["x0"] < 60 and w["size"] > 20 and t.isdigit():
            rno = int(t)
            dl = next((_z2h(v["text"]) for v in ws if 80 < v["x0"] < 130 and 0 < v["top"] - w["top"] < 25
                       and re.fullmatch(r"\d{1,2}:\d{2}", _z2h(v["text"]))), "")
            title = "".join(v["text"] for v in sorted(ws, key=lambda v: v["x0"])
                            if 140 < v["x0"] < 280 and abs(v["top"] - w["top"] - 10) < 8)
            heads.append((w["top"], rno, dl, title))
    return sorted(heads)


def _runs(text):
    """今節成績欄 "4三15" "5 六22" → [(着, コース, ST)]。"""
    out = []
    for m in re.finditer(r"([1-6]|[FLＦＬ欠妨転落エ失沈不])\s*([一二三四五六])\s*(\d{2})", _z2h(text)):
        fin = int(m.group(1)) if m.group(1).isdigit() else np.nan
        out.append((fin, KANJI_COURSE[m.group(2)], int(m.group(3)) / 100))
    return out


def parse_pdf(path, date_yyyymmdd, jcd, venue, grade="", day_no=None, race_type="予選",
              distance_m=1800):
    import pdfplumber

    d = str(date_yyyymmdd)
    jcd = str(jcd).zfill(2)
    prog, rl, runs = [], [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            ws = _words(page)
            heads = _race_headers(ws)
            anchors = sorted((w for w in ws if 60 <= w["x0"] < 80 and re.fullmatch(r"\d{4}", w["text"])),
                             key=lambda w: w["top"])
            by_race = {}
            for a in anchors:
                above = [h for h in heads if h[0] < a["top"]]
                if not above:
                    continue
                by_race.setdefault(above[-1], []).append(a)
            for (hy, rno, dl, title), rows in by_race.items():
                if len(rows) != 6:
                    continue
                rk = f"{d}_{jcd}_{rno}"
                for lane, a in enumerate(rows, start=1):
                    y = a["top"]
                    # 2行目（期別・年齢・3連率など）の高さ: 登番の下の「〇〇期」の語の位置
                    ki = [w for w in ws if 60 <= w["x0"] < 80 and w["text"].endswith("期") and 4 < w["top"] - y < 14]
                    yb = ki[0]["top"] if ki else y + 9
                    left = _text(ws, y, 165, 205) + _text(ws, yb, 165, 205)
                    fl = re.findall(r"([FL])(\d)", _z2h(left))
                    f_count = sum(int(n) for k, n in fl if k == "F")
                    l_count = sum(int(n) for k, n in fl if k == "L")
                    weight = re.match(r"\s*(\d{2})", _z2h(_text(ws, y, 165, 200)))
                    age = re.match(r"\s*(\d{2})", _z2h(_text(ws, yb, 165, 200)))
                    nat_win, (nat2, rec2, loc2) = _rate_line(_text(ws, y, 200, 282))
                    rec_win, (nat3, rec3, loc3) = _rate_line(_text(ws, yb, 200, 282))
                    st = re.match(r"(\d{2})", _text(ws, y, 338, 352))
                    motor = [w for w in ws if 425 <= w["x0"] < 446 and w["size"] > 14 and y - 4 < w["top"] < yb + 4]
                    boat = [w for w in ws if 514 <= w["x0"] < 533 and w["size"] > 12 and y - 4 < w["top"] < yb + 4]
                    name = _text(ws, y + 1, 90, 165, tol=4)
                    prog.append({
                        "race_date": d, "jcd": jcd, "venue": venue, "race_no": rno, "race_key": rk,
                        "meet_title": title, "day_no": day_no, "race_type": race_type,
                        "fixed_entry": 0, "distance_m": distance_m, "deadline": dl,
                        "lane": lane, "racer_id": a["text"], "racer_name": name.replace("♥", "").strip(),
                        "age": _float(age.group(1)) if age else np.nan,
                        "branch": "", "weight": _float(weight.group(1)) if weight else np.nan,
                        "racer_class": _z2h(_text(ws, y, 50, 66)),
                        "national_win_rate": nat_win,
                        "national_2ren": nat2, "local_win_rate": np.nan, "local_2ren": loc2,
                        "motor_no": _z2h(motor[0]["text"]) if motor else "",
                        "motor_2ren": _float(_text(ws, yb, 444, 462)),
                        "boat_no": _z2h(boat[0]["text"]) if boat else "",
                        "boat_2ren": _float(_text(ws, yb, 532, 552)),
                        "meet_results_raw": "", "hayami": "",
                    })
                    rl.append({
                        "race_key": rk, "lane": lane, "grade": grade, "is_final_day": 0,
                        "avg_st": int(st.group(1)) / 100 if st else np.nan,
                        "f_count": f_count, "l_count": l_count,
                        "national_3ren": nat3, "local_3ren": loc3,
                        "motor_3ren": np.nan, "boat_3ren": np.nan,
                    })
                    for i, (fin, course, stv) in enumerate(_runs(_text(ws, y, 550, 735) + " "
                                                                + _text(ws, yb, 550, 735))):
                        runs.append({"racer_id": a["text"], "n": i + 1, "finish": fin, "course": course,
                                     "st": stv})
    return pd.DataFrame(prog), pd.DataFrame(rl), pd.DataFrame(runs)
