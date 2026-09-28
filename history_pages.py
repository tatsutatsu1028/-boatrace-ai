"""
出走表・直前情報ページから、公式ダウンロードファイル（K/B）に無い項目を集める。

対象レースは競走成績（k_results）に載っているレース。新しい日付から順に取り、
取得済みは data/history/pages/ にあるので、止まっても次回は続きから再開する。

1レースあたり2ページ（出走表・直前情報）。取得間隔は polite_http で
全体として開始間隔3.4秒以上（毎秒0.3リクエスト以下）に制限している。

取る項目（1艇1行、race_key + lane）:
  出走表   : 級別、F数・L数、平均ST、全国/当地の勝率・2連率・3連率、
             モーター/ボートのNo・2連率・3連率、支部・年齢・体重、
             レース共通: グレード、節の何日目（初日〜最終日）、節の日数、
             レース名（予選/準優勝戦/優勝戦…）、距離、進入固定、安定板使用、締切時刻
  直前情報 : 体重、調整重量、展示タイム、チルト、プロペラ（新）、部品交換、
             スタート展示の進入コース・ST、
             レース共通: 気温・天候・風速・風向（アイコン番号）・水温・波高（何R時点か）

使い方:
  python history_pages.py --start 20250927 --end 20260926 --stop-at 03:50
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

import history_store as store
import polite_http
from official_fetcher import (
    _expand_table, _find_table, _main_race_rows, _parse_racelist_row,
    _before_rows, _parse_before_row, _parse_parts_exchange,
)

BASE = "https://www.boatrace.jp/owpc/pc/race"
FAILED_LOG = store.ROOT / "pages_failed.csv"
MAX_ATTEMPTS = 3

GRADE_LABELS = {
    "SG": "SG", "G1": "G1", "G2": "G2", "G3": "G3", "ippan": "一般", "IPPAN": "一般",
}


def norm(s):
    s = unicodedata.normalize("NFKC", "" if s is None else str(s))
    return re.sub(r"\s+", " ", s).strip()


def nums(s):
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", norm(s).replace(",", ""))]


# ---------------------------------------------------------------
# 出走表・直前情報で共通のレース情報（見出し・日付タブ・レース名）
# ---------------------------------------------------------------
def parse_race_header(soup, html, date_yyyymmdd, rno):
    out = {}
    title = soup.select_one(".heading2_title")
    if title is not None:
        cls = " ".join(title.get("class", []))
        m = re.search(r"\bis-(SG|G1|G2|G3|ippan|IPPAN)([ab]?)\b", cls)
        if m:
            out["grade"] = GRADE_LABELS.get(m.group(1), m.group(1))
            out["grade_class"] = f"is-{m.group(1)}{m.group(2)}"
        # 女子戦・ルーキー等の付加クラス
        extra = [c for c in title.get("class", []) if c.startswith("is-") and not re.match(r"is-(SG|G[123]|ippan)", c)]
        out["grade_extra"] = " ".join(extra)
        name = title.select_one(".heading2_titleName")
        out["meet_title"] = norm(name.get_text(" ")) if name else ""

    # 日付タブ: 「9月29日 初日」「10月1日 3日目」…「最終日」。is-active2 が当日。
    tabs = soup.select(".tab2_tabs li")
    if tabs:
        out["meet_days"] = len(tabs)
        for i, li in enumerate(tabs, 1):
            if "is-active2" in (li.get("class") or []):
                out["day_no"] = i
                sp = li.find_all("span")
                out["day_label"] = norm(sp[-1].get_text()) if sp else ""
                break
        out["is_final_day"] = int(out.get("day_no") == len(tabs))

    # 締切時刻・レース名・距離・進入固定・安定板（本文テキストから読む）
    text = norm(soup.get_text(" "))
    m = re.search(r"締切予定時刻((?:\s+\d{1,2}:\d{2})+)\s+(.*?)\s+(\d{3,4})m\s*(.*?)\s*出走表\s", text)
    if m:
        times = m.group(1).split()
        if 1 <= int(rno) <= len(times):
            out["deadline"] = times[int(rno) - 1]
        out["race_name"] = m.group(2).split(" ")[-1] if m.group(2) else ""
        out["distance_m"] = int(m.group(3))
        flags = m.group(4)
        out["fixed_entry"] = int("進入固定" in flags or "進入固定" in m.group(2))
        out["stabilizer"] = int("安定板" in flags or "安定板" in m.group(2))
    out["race_category"] = race_category(out.get("race_name", ""))
    return out


def race_category(name):
    n = name or ""
    if "準優" in n:
        return "準優勝戦"
    if "優勝戦" in n:
        return "優勝戦"
    if "予選" in n:
        return "予選"
    if any(w in n for w in ("選抜", "特選", "特賞", "ドリーム", "選抜戦")):
        return "特別選抜"
    if "一般" in n:
        return "一般"
    return "その他"


# ---------------------------------------------------------------
# 出走表
# ---------------------------------------------------------------
def parse_racelist(html, date_yyyymmdd, rno):
    soup = BeautifulSoup(html, "lxml")
    race = parse_race_header(soup, html, date_yyyymmdd, rno)
    table = _find_table(soup, ["ボートレーサー", "全国", "当地", "モーター", "ボート"])
    boats = {}
    if table is not None:
        rows = _main_race_rows(table)
        for ln in range(1, 7):
            row = rows.get(ln, [])
            rec = _parse_racelist_row(ln, row)
            racer_i = next((i for i, c in enumerate(row) if re.search(r"\d{4}\s*/\s*[AB][12]", norm(c))), None)
            if racer_i is not None:
                def at(k):
                    i = racer_i + k
                    return row[i] if 0 <= i < len(row) else ""
                prof = norm(at(0))
                m = re.search(r"([^\s/]+)/([^\s/]+)\s+(\d+)歳/(\d+(?:\.\d+)?)kg", prof)
                if m:
                    rec["branch"], rec["birthplace"] = m.group(1), m.group(2)
                    rec["age"], rec["weight_racelist"] = int(m.group(3)), float(m.group(4))
                for key, k in (("national", 2), ("local", 3)):
                    v = nums(at(k))
                    if len(v) >= 3:
                        rec[f"{key}_win_rate"], rec[f"{key}_2ren"], rec[f"{key}_3ren"] = v[:3]
                for key, k in (("motor", 4), ("boat", 5)):
                    v = nums(at(k))
                    if len(v) >= 3:
                        rec[f"{key}_no"] = int(v[0])
                        rec[f"{key}_2ren"], rec[f"{key}_3ren"] = v[1], v[2]
                fl = norm(at(1))
                if "f_count" not in rec and re.search(r"F\s*\d", fl):
                    rec["f_count"] = int(re.search(r"F\s*(\d+)", fl).group(1))
            boats[ln] = rec
    return race, boats


# ---------------------------------------------------------------
# 直前情報
# ---------------------------------------------------------------
def _col_index(table, label):
    for row in _expand_table(table):
        if row and re.fullmatch(r"[1-6]", norm(row[0])):
            break
        for i, c in enumerate(row):
            if norm(c).replace(" ", "") == label:
                return i
    return None


def parse_start_exhibition(soup):
    """スタート展示: 上から順にコース1〜6。各行に艇番とSTがある。"""
    out = {}
    for course, box in enumerate(soup.select(".table1_boatImage1"), 1):
        num = box.select_one(".table1_boatImage1Number")
        tm = box.select_one(".table1_boatImage1Time")
        if num is None:
            continue
        m = re.search(r"[1-6]", norm(num.get_text()))
        if not m:
            continue
        lane = int(m.group(0))
        raw = norm(tm.get_text()) if tm else ""
        st = None
        sm = re.fullmatch(r"(F|L)?\s*(\d?\.\d{2})", raw)
        if sm:
            v = float(sm.group(2))
            st = -v if sm.group(1) == "F" else (None if sm.group(1) == "L" else v)
        out[lane] = {"exhibition_course": course, "exhibition_st": st, "exhibition_st_raw": raw}
    return out


def parse_weather(soup):
    out = {}
    w = soup.select_one(".weather1")
    if w is None:
        return out
    title = norm((w.select_one(".weather1_title") or w).get_text())
    m = re.search(r"(\d{1,2})R時点", title)
    out["weather_as_of_race"] = int(m.group(1)) if m else None

    def unit(cls):
        return w.select_one(f".weather1_bodyUnit.{cls}")

    def data(u):
        d = u.select_one(".weather1_bodyUnitLabelData") if u else None
        v = nums(d.get_text()) if d else []
        return v[0] if v else None

    def img_no(u, prefix):
        img = u.select_one(".weather1_bodyUnitImage") if u else None
        for c in (img.get("class", []) if img else []):
            m2 = re.fullmatch(rf"is-{prefix}(\d+)", c)
            if m2:
                return int(m2.group(1))
        return None

    u = unit("is-direction")
    out["temperature"] = data(u)                   # マイナスもそのまま
    out["stadium_direction_code"] = img_no(u, "direction")
    u = unit("is-weather")
    t = u.select_one(".weather1_bodyUnitLabelTitle") if u else None
    out["weather"] = norm(t.get_text()) if t else ""
    out["weather_code"] = img_no(u, "weather")
    out["wind_speed"] = data(unit("is-wind"))
    out["wind_direction_code"] = img_no(unit("is-windDirection"), "wind")
    out["water_temperature"] = data(unit("is-waterTemperature"))
    out["wave_cm"] = data(unit("is-wave"))
    return out


def parse_beforeinfo(html):
    soup = BeautifulSoup(html, "lxml")
    boats = {}
    table = _find_table(soup, ["ボートレーサー", "体重", "展示", "タイム", "チルト"])
    if table is not None:
        rows = _before_rows(table)
        parts_col = _col_index(table, "部品交換")
        prop_col = _col_index(table, "プロペラ")
        for ln, row in rows.items():
            rec = _parse_before_row(ln, row, parts_col=parts_col)
            if prop_col is not None and prop_col < len(row):
                rec["propeller"] = norm(row[prop_col])
                rec["propeller_new"] = int("新" in rec["propeller"])
            boats[ln] = rec
        # 部品交換は <ul class="labelGroup1"><li>…</li></ul> で並ぶので、
        # 表の展開結果ではなく要素から直接読み直す（複数部品を「、」区切り）。
        for i, tb in enumerate(table.find_all("tbody"), 1):
            ul = tb.select_one("ul.labelGroup1")
            lane_td = tb.find("td")
            m = re.search(r"[1-6]", norm(lane_td.get_text())) if lane_td else None
            if not (ul is not None and m):
                continue
            ln = int(m.group(0))
            items = [norm(li.get_text()) for li in ul.find_all("li")]
            items = [x for x in items if x]
            rec = boats.setdefault(ln, {"lane": ln})
            rec["parts_exchange"] = "、".join(items)
            rec["parts_exchanged"] = int(bool(items))
            rec["parts_exchange_count"] = len(items)
            # 調整重量（体重の下の段）
            tds = tb.find_all("td")
            for j, td in enumerate(tds):
                if norm(td.get_text()) == "ST" and j > 0:
                    v = nums(tds[j - 1].get_text())
                    if v:
                        rec["adjust_weight"] = v[0]
                    break
    st = parse_start_exhibition(soup)
    for ln, v in st.items():
        boats.setdefault(ln, {"lane": ln}).update(v)
    wx = parse_weather(soup)
    return boats, wx


# ---------------------------------------------------------------
# 1レース分
# ---------------------------------------------------------------
class PageError(Exception):
    pass


def _fetch(kind, hd, jcd, rno):
    url = f"{BASE}/{kind}?hd={hd}&jcd={jcd}&rno={int(rno)}"
    r = polite_http.get(url, timeout=40)
    if r.status_code != 200:
        raise PageError(f"{kind} HTTP {r.status_code}")
    return r.text


def collect_race(hd, jcd, rno):
    rl_html = _fetch("racelist", hd, jcd, rno)
    race, rl_boats = parse_racelist(rl_html, hd, rno)
    if not any(b.get("racer_id") for b in rl_boats.values()):
        raise PageError("racelist: 選手が読めない")
    bi_html = _fetch("beforeinfo", hd, jcd, rno)
    bi_boats, wx = parse_beforeinfo(bi_html)
    rows = []
    for ln in range(1, 7):
        rec = {"race_date": hd, "jcd": jcd, "race_no": int(rno),
               "race_key": f"{hd}_{jcd}_{int(rno)}", "lane": ln}
        rec.update(race)
        rec.update(wx)
        rec.update({k: v for k, v in rl_boats.get(ln, {}).items() if k != "lane"})
        rec.update({k: v for k, v in bi_boats.get(ln, {}).items() if k != "lane"})
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["fetched_at"] = polite_http.now_jst().strftime("%Y-%m-%d %H:%M")
    return df


# ---------------------------------------------------------------
# 対象レース・失敗記録
# ---------------------------------------------------------------
def target_races(start, end):
    k = store.read_kind("k_results", start, end, columns=["race_date", "jcd", "race_no", "race_key"])
    t = k.drop_duplicates("race_key")
    t = t.assign(_rno=pd.to_numeric(t["race_no"])).sort_values(["race_date", "jcd", "_rno"],
                                                               ascending=[False, True, True])
    return list(t[["race_date", "jcd", "race_no", "race_key"]].itertuples(index=False, name=None))


def load_failed():
    if FAILED_LOG.exists():
        return pd.read_csv(FAILED_LOG, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=["race_key", "attempts", "last_error", "last_at"])


def save_failed(df):
    FAILED_LOG.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values("race_key").to_csv(FAILED_LOG, index=False, encoding="utf-8")


# ---------------------------------------------------------------
# 既存の日次収集（Collect History）と重ならないようにする
# ---------------------------------------------------------------
def other_crawler_running():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/collect_history_workflow.yml/runs",
            params={"status": "in_progress", "per_page": 1},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        return r.ok and r.json().get("total_count", 0) > 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------
# 途中保存（コミット）
# ---------------------------------------------------------------
def checkpoint(cmd):
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=True, check=False, timeout=300)
    except Exception as e:  # noqa: BLE001
        print(f"[PAGES] checkpoint失敗: {e}", flush=True)


def seal_complete_months(start, end):
    """対象レースがすべて取得済み（または取得不能と確定）の過去月を圧縮する。"""
    done = store.existing_race_keys("pages")
    failed = load_failed()
    gave_up = set(failed.loc[pd.to_numeric(failed["attempts"], errors="coerce") >= MAX_ATTEMPTS, "race_key"])
    cur = polite_http.now_jst().strftime("%Y%m")
    targets = pd.DataFrame(target_races(start, end), columns=["race_date", "jcd", "race_no", "race_key"])
    if targets.empty:
        return
    targets["ym"] = targets["race_date"].str[:6]
    for ym, part in targets.groupby("ym"):
        if ym >= cur:
            continue
        if set(part["race_key"]) <= (done | gave_up):
            if store.seal("pages", ym[:4], ym[4:]):
                print(f"[PAGES] 圧縮 {ym}", flush=True)


# ---------------------------------------------------------------
# メイン
# ---------------------------------------------------------------
def run(start, end, stop_at=None, max_minutes=0, workers=3, checkpoint_cmd="", checkpoint_minutes=60, limit=0):
    deadline = time.time() + max_minutes * 60 if max_minutes else None
    if stop_at:
        t = polite_http.deadline_from_stop_at(stop_at)
        deadline = min(deadline, t) if deadline else t
    done = store.existing_race_keys("pages")
    failed = load_failed()
    attempts = {r.race_key: int(r.attempts or 0) for r in failed.itertuples()}
    targets = [t for t in target_races(start, end)
               if t[3] not in done and attempts.get(t[3], 0) < MAX_ATTEMPTS]
    if limit:
        targets = targets[:limit]
    print(f"[PAGES] 対象 {len(targets)}レース（取得済み {len(done)}）", flush=True)

    buf, ok, ng = [], 0, 0
    started = last_cp = last_report = time.time()
    fail_rows = {r.race_key: r._asdict() for r in failed.itertuples(index=False)}
    lock = threading.Lock()

    def flush():
        nonlocal buf
        with lock:
            if buf:
                store.append("pages", pd.concat(buf, ignore_index=True))
                buf = []
            if fail_rows:
                save_failed(pd.DataFrame(list(fail_rows.values())))

    def one(t):
        hd, jcd, rno, key = t
        try:
            return key, collect_race(hd, jcd, rno), ""
        except Exception as e:  # noqa: BLE001
            return key, None, f"{type(e).__name__}: {str(e)[:150]}"

    i = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while i < len(targets):
            if deadline and time.time() >= deadline:
                print("[PAGES] 停止時刻になったので終了（次回続きから）", flush=True)
                break
            if other_crawler_running():
                print("[PAGES] 日次収集（Collect History）が実行中のため5分待機", flush=True)
                flush()
                time.sleep(300)
                continue
            batch = targets[i:i + 12]
            i += len(batch)
            futs = [ex.submit(one, t) for t in batch]
            for fu in as_completed(futs):
                key, df, err = fu.result()
                if df is not None:
                    with lock:
                        buf.append(df)
                    fail_rows.pop(key, None)
                    ok += 1
                else:
                    ng += 1
                    prev = fail_rows.get(key, {})
                    fail_rows[key] = {"race_key": key, "attempts": int(prev.get("attempts") or 0) + 1,
                                      "last_error": err, "last_at": polite_http.now_jst().strftime("%Y-%m-%d %H:%M")}
                    if ng <= 5 or ng % 50 == 0:
                        print(f"[PAGES] 失敗 {key}: {err}", flush=True)
            if time.time() - last_report >= 300:
                el = (time.time() - started) / 3600
                print(f"[PAGES] 取得{ok} 失敗{ng} 残り{len(targets) - i} "
                      f"({ok / max(el, 1e-9):.0f}レース/時, {polite_http.request_count() / max(el * 3600, 1e-9):.3f}req/s) "
                      f"最新 {batch[-1][3]}", flush=True)
                last_report = time.time()
            if time.time() - last_cp >= checkpoint_minutes * 60:
                flush()
                checkpoint(checkpoint_cmd)
                last_cp = time.time()
    flush()
    el = (time.time() - started) / 3600
    print(f"[PAGES] 終了 取得{ok} 失敗{ng} {el:.2f}時間 リクエスト{polite_http.request_count()}回 "
          f"({polite_http.request_count() / max(el * 3600, 1e-9):.3f}req/s)", flush=True)
    return ok, ng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--stop-at", default="", help="この時刻(JST HH:MM)で止める")
    ap.add_argument("--max-minutes", type=float, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--checkpoint-cmd", default="")
    ap.add_argument("--checkpoint-minutes", type=float, default=60)
    ap.add_argument("--limit", type=int, default=0, help="動作確認用: このレース数で止める")
    ap.add_argument("--selftest", default="", help="hd,jcd,rno を1レースだけ取得して表示")
    args = ap.parse_args()
    if args.selftest:
        hd, jcd, rno = args.selftest.split(",")
        df = collect_race(hd, jcd.zfill(2), int(rno))
        pd.set_option("display.width", 250)
        print(df.T.to_string())
        return
    if not args.start:
        ap.error("--start が必要です")
    end = args.end or (polite_http.now_jst() - timedelta(days=1)).strftime("%Y%m%d")
    run(args.start, end, stop_at=args.stop_at or None, max_minutes=args.max_minutes,
        workers=args.workers, checkpoint_cmd=args.checkpoint_cmd,
        checkpoint_minutes=args.checkpoint_minutes, limit=args.limit)
    seal_complete_months(args.start, end)


if __name__ == "__main__":
    main()
