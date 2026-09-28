"""
公式ダウンロードファイル（競走成績 K / 番組表 B）の取得と解析。

配布元: https://www.boatrace.jp/owpc/pc/extra/data/download.html
  競走成績 https://www1.mbrace.or.jp/od2/K/YYYYMM/kYYMMDD.lzh
  番組表   https://www1.mbrace.or.jp/od2/B/YYYYMM/bYYMMDD.lzh
1日1ファイル（全場分、LZH圧縮、Shift_JIS の固定幅テキスト）。

解析結果は history_store へ月別に保存する:
  k_results  競走成績 1艇1行（着順1〜6・実際の進入コース・ST・展示タイム・レースタイム・決まり手・天候など）
  k_payouts  払戻 1券種・1組番1行（単勝〜3連単、人気）
  b_programs 番組表 1艇1行（級別・勝率・2連率・モーター/ボート2連率・今節成績など）

取得済みの日付は data/history/official_dates.csv に記録し、再実行時は飛ばす。

使い方:
  python official_download.py --start 20240927 --end 20260926
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import history_store as store
import polite_http

VENUES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖",
    "07": "蒲郡", "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山",
    "19": "下関", "20": "若松", "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}

DATES_LOG = store.ROOT / "official_dates.csv"
URL = "https://www1.mbrace.or.jp/od2/{kind}/{yyyymm}/{prefix}{yymmdd}.lzh"


def nfkc(s):
    return unicodedata.normalize("NFKC", s or "")


def compact(s):
    return re.sub(r"\s+", "", nfkc(s))


# ---------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------
def download_text(kind, date_yyyymmdd):
    """LZHを取得して解凍したテキストを返す。ファイルが無ければ None。"""
    import lhafile

    d = str(date_yyyymmdd)
    url = URL.format(kind=kind, yyyymm=d[:6], prefix=kind.lower(), yymmdd=d[2:])
    r = polite_http.get(url, timeout=60, binary=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    f = lhafile.Lhafile(io.BytesIO(r.content))
    texts = []
    for info in f.infolist():
        texts.append(f.read(info.filename).decode("cp932", errors="replace"))
    return "\n".join(texts)


# ---------------------------------------------------------------
# 共通: 場ごとのブロックに分ける
# ---------------------------------------------------------------
def _venue_blocks(text, tag):
    """'24KBGN' 〜 '24KEND' の区切りで (jcd, 行リスト) を返す。"""
    lines = text.replace("\r", "").split("\n")
    cur, buf = None, []
    for ln in lines:
        m = re.match(rf"^(\d{{2}}){tag}BGN", ln.strip())
        if m:
            cur, buf = m.group(1), []
            continue
        if cur and re.match(rf"^\d{{2}}{tag}END", ln.strip()):
            yield cur, buf
            cur, buf = None, []
            continue
        if cur:
            buf.append(ln)
    if cur and buf:
        yield cur, buf


def _day_no(s):
    m = re.search(r"第\s*(\d+)\s*日", nfkc(s))
    return int(m.group(1)) if m else None


def _race_time_sec(s):
    m = re.fullmatch(r"(\d+)\.(\d{2})\.(\d)", s or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 10


# ---------------------------------------------------------------
# 競走成績（K）
# ---------------------------------------------------------------
K_RACE_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(.*?)\s*H(\d{3,4})m\s*(.*)$")
K_BOAT = re.compile(r"^\s*(\S{1,2})\s+([1-6])\s+(\d{4})\s(.{8})\s*(.*)$")
BET_TYPES = ["単勝", "複勝", "2連単", "2連複", "拡連複", "3連単", "3連複"]


def _parse_k_boat_rest(rest):
    """登番・氏名より右（モーター ボート 展示 進入 ST レースタイム）を読む。"""
    toks = rest.split()
    out = {"motor_no": None, "boat_no": None, "exhibition_time": None,
           "course": None, "st_raw": "", "race_time_raw": ""}
    i = 0
    if i < len(toks) and toks[i].isdigit():
        out["motor_no"] = toks[i]; i += 1
    if i < len(toks) and toks[i].isdigit():
        out["boat_no"] = toks[i]; i += 1
    if i < len(toks) and re.fullmatch(r"\d\.\d{2}", toks[i]):
        out["exhibition_time"] = toks[i]; i += 1
    if i < len(toks) and re.fullmatch(r"[1-6]", toks[i]):
        out["course"] = toks[i]; i += 1
    if i < len(toks) and re.fullmatch(r"[FLK]?\d?\.\d{2}|[FLK]\S*", toks[i]):
        out["st_raw"] = toks[i]; i += 1
    rt = " ".join(toks[i:])
    out["race_time_raw"] = rt
    return out


def _st_value(raw):
    """ST文字列 → 数値。フライングは負の値（例 F.02 → -0.02）、出遅れ等は空。"""
    m = re.fullmatch(r"([FL]?)(\d?\.\d{2})", raw or "")
    if not m:
        return None
    v = float(m.group(2))
    return -v if m.group(1) == "F" else (None if m.group(1) == "L" else v)


def parse_k(text, date_yyyymmdd):
    boats, pays = [], []
    d = str(date_yyyymmdd)
    for jcd, lines in _venue_blocks(text, "K"):
        meet_title, day_no = "", None
        # 見出し: 「大　村［成績］     10/ 1      スポーツニッポン杯　  第 3日」
        for ln in lines[:6]:
            if "成績" in ln and "［" in ln:
                # 節名に「第4回…」のように「第」を含むことがあるので、
                # 行末の「第 N日」だけを除いた日付の後ろ全体を節名とする。
                hm = re.search(r"[［\[]成績[］\]]\s+\d+/\s*\d+\s+(.*?)\s+第\s*\d+\s*日\s*$", nfkc(ln).strip())
                meet_title = compact(hm.group(1)) if hm else ""
                day_no = _day_no(ln)
                break
        race = None
        in_rows = False
        last_bet = None
        for ln in lines:
            s = ln.rstrip()
            m = K_RACE_HEAD.match(nfkc(s))
            if m and ("風" in s or "波" in s or "H" in s):
                rno, head, dist, wx = m.groups()
                head_c = compact(head)
                fixed = "進入固定" in head_c
                race_type = head_c.replace("進入固定", "")
                wm = re.search(r"^(\S+)\s+風\s*(\S*?)\s*(\d+)m\s+波\s*(\d+)cm", nfkc(wx).strip())
                race = {
                    "race_date": d, "jcd": jcd, "venue": VENUES.get(jcd, jcd),
                    "race_no": int(rno), "race_key": f"{d}_{jcd}_{int(rno)}",
                    "meet_title": meet_title, "day_no": day_no,
                    "race_type": race_type, "fixed_entry": int(fixed),
                    "distance_m": int(dist),
                    "weather": wm.group(1) if wm else "",
                    "wind_direction": (wm.group(2) if wm else "").strip(),
                    "wind_speed": int(wm.group(3)) if wm else None,
                    "wave_cm": int(wm.group(4)) if wm else None,
                    "kimarite": "",
                }
                in_rows = False
                last_bet = None
                continue
            if race is None:
                continue
            if "着" in s and "艇" in s and "登番" in s:
                km = re.search(r"ﾚｰｽﾀｲﾑ\s*(\S+)", s)
                race["kimarite"] = compact(km.group(1)) if km else ""
                continue
            if s.startswith("---") or s.strip().startswith("-----"):
                in_rows = True
                continue
            if in_rows:
                bm = K_BOAT.match(s)
                if bm:
                    pos, lane, toban, name, rest = bm.groups()
                    pos = pos.strip()
                    fin = int(pos) if pos.isdigit() and 1 <= int(pos) <= 6 else None
                    r = dict(race)
                    r.update({
                        "lane": int(lane), "racer_id": toban, "racer_name": compact(name),
                        "finish": fin, "finish_raw": pos,
                    })
                    r.update(_parse_k_boat_rest(rest))
                    r["st"] = _st_value(r["st_raw"])
                    r["race_time_sec"] = _race_time_sec(r["race_time_raw"])
                    boats.append(r)
                    continue
                if s.strip() == "":
                    in_rows = False
                    continue
            # 払戻
            ns = nfkc(s).strip()
            if not ns:
                continue
            bet = next((b for b in BET_TYPES if ns.startswith(b)), None)
            body = ns[len(bet):] if bet else ns
            if bet:
                last_bet = bet
            elif not (last_bet and re.match(r"^\d(-\d){0,2}\s", ns)):
                continue
            if last_bet is None:
                continue
            for pm in re.finditer(r"(\d(?:-\d){0,2})\s+(\d+)(?:\s+人気\s+(\d+))?", body):
                pays.append({
                    "race_date": d, "jcd": jcd, "race_no": race["race_no"],
                    "race_key": race["race_key"], "bet_type": last_bet,
                    "combo": pm.group(1), "payout": int(pm.group(2)),
                    "popularity": int(pm.group(3)) if pm.group(3) else None,
                })
            for sp in ("特払い", "不成立", "返還"):
                if sp in body and not re.search(r"\d-\d", body):
                    pays.append({
                        "race_date": d, "jcd": jcd, "race_no": race["race_no"],
                        "race_key": race["race_key"], "bet_type": last_bet,
                        "combo": sp, "payout": None, "popularity": None,
                    })
    return _ints(pd.DataFrame(boats), ["finish", "course", "wind_speed", "wave_cm", "day_no"]), \
        _ints(pd.DataFrame(pays), ["popularity", "payout"])


def _ints(df, cols):
    """欠損を含む整数列が 1.0 のような小数で保存されないようにする。"""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


# ---------------------------------------------------------------
# 番組表（B）
# ---------------------------------------------------------------
B_RACE_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(.*?)\s*H(\d{3,4})m\s*(.*)$")
# 番組表の1艇の行は固定幅。モーター/ボートNoが3桁になると前の列と
# 空白なしでくっつく（例 "39.74117 32.11"）ので、空白区切りではなく位置で切る。
#   "1 5015高橋竜矢28広島52A1" + 全国勝率(5) 全国2率(6) 当地勝率(5) 当地2率(6)
#   + モーターNo(3) モーター2率(6) ボートNo(3) ボート2率(6) + 空白1 今節成績(12) 早見(2)
B_BOAT_HEAD = re.compile(r"^([1-6]) (\d{4})(.{4})(\d{2})(.{2})(\d{2})([AB][12])(.*)$")
B_WIDTHS = [("national_win_rate", 5), ("national_2ren", 6), ("local_win_rate", 5), ("local_2ren", 6),
            ("motor_no", 3), ("motor_2ren", 6), ("boat_no", 3), ("boat_2ren", 6)]


def _parse_b_boat(line):
    m = B_BOAT_HEAD.match(line)
    if not m:
        return None
    g = m.groups()
    rest = g[7]
    out = {
        "lane": int(g[0]), "racer_id": g[1], "racer_name": compact(g[2]),
        "age": int(g[3]), "branch": compact(g[4]), "weight": int(g[5]), "racer_class": g[6],
    }
    pos = 0
    for name, w in B_WIDTHS:
        v = rest[pos:pos + w].strip()
        pos += w
        if name.endswith("_no"):
            out[name] = v
        else:
            try:
                out[name] = float(v)
            except ValueError:
                return None
    tail = rest[pos:]
    # 今節成績（12文字 = 6日×2走。着順が1文字ずつ入り、空白は未出走）と早見（2文字）
    out["meet_results_raw"] = tail[1:13].rstrip()
    out["hayami"] = tail[13:15].strip()
    return out


def parse_b(text, date_yyyymmdd):
    rows = []
    d = str(date_yyyymmdd)
    for jcd, lines in _venue_blocks(text, "B"):
        meet_title, day_no = "", None
        for ln in lines[:4]:
            n = nfkc(ln)
            if "ボートレース" in n and "月" in n and "日" in n:
                day_no = _day_no(n)
                parts = re.split(r"\s{2,}", ln.strip())
                if len(parts) >= 3:
                    meet_title = compact(parts[2])
                break
        race = None
        for ln in lines:
            n = nfkc(ln)
            m = B_RACE_HEAD.match(n)
            if m and "締切" in n:
                rno, head, dist, tail = m.groups()
                head_c = compact(head)
                dl = re.search(r"(\d{1,2}):(\d{2})", tail)
                race = {
                    "race_date": d, "jcd": jcd, "venue": VENUES.get(jcd, jcd),
                    "race_no": int(rno), "race_key": f"{d}_{jcd}_{int(rno)}",
                    "meet_title": meet_title, "day_no": day_no,
                    "race_type": head_c.replace("進入固定", ""),
                    "fixed_entry": int("進入固定" in head_c),
                    "distance_m": int(dist),
                    "deadline": f"{int(dl.group(1)):02d}:{dl.group(2)}" if dl else "",
                }
                continue
            if race is None:
                continue
            boat = _parse_b_boat(ln.rstrip("\r"))
            if boat is None:
                continue
            r = dict(race)
            r.update(boat)
            rows.append(r)
    return _ints(pd.DataFrame(rows), ["day_no"])


def debug_block(kind, date_yyyymmdd, jcd, n=45):
    """解析できない場の生テキストを確認する（ログ出力用）。"""
    text = download_text(kind, date_yyyymmdd) or ""
    for code, lines in _venue_blocks(text, kind):
        if code == jcd:
            for ln in lines[:n]:
                print(repr(ln))


# ---------------------------------------------------------------
# 取得済み日付の記録
# ---------------------------------------------------------------
def load_dates_log():
    if DATES_LOG.exists():
        return pd.read_csv(DATES_LOG, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=["race_date", "kind", "status", "venues", "races", "rows", "fetched_at"])


def save_dates_log(df):
    DATES_LOG.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values(["race_date", "kind"]).to_csv(DATES_LOG, index=False, encoding="utf-8")


def _daterange_desc(start, end):
    s = datetime.strptime(str(start), "%Y%m%d")
    e = datetime.strptime(str(end), "%Y%m%d")
    while e >= s:
        yield e.strftime("%Y%m%d")
        e -= timedelta(days=1)


def _fetch_parse(d, kind):
    """1ファイル取得・解析（スレッドで実行。保存はメインスレッドで行う）。"""
    rec = {"race_date": d, "kind": kind, "status": "", "venues": "", "races": "", "rows": "",
           "fetched_at": polite_http.now_jst().strftime("%Y-%m-%d %H:%M")}
    frames = {}
    try:
        text = download_text(kind, d)
        if text is None:
            rec["status"] = "no_file"
        elif kind == "K":
            boats, pays = parse_k(text, d)
            frames = {"k_results": boats, "k_payouts": pays}
            main = boats
        else:
            main = parse_b(text, d)
            frames = {"b_programs": main}
        if frames:
            rec.update(status="ok", venues=main["jcd"].nunique() if len(main) else 0,
                       races=main["race_key"].nunique() if len(main) else 0, rows=len(main))
    except Exception as e:  # noqa: BLE001
        rec["status"] = f"error: {type(e).__name__}: {str(e)[:120]}"
    return rec, frames


def checkpoint(cmd):
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=True, check=False, timeout=300)
    except Exception as e:  # noqa: BLE001
        print(f"[DL] checkpoint失敗: {e}", flush=True)


def run(start, end, kinds=("K", "B"), deadline=None, retry_missing_days=7, workers=3,
        checkpoint_cmd="", checkpoint_minutes=30):
    from concurrent.futures import ThreadPoolExecutor

    log = load_dates_log()
    recent = (datetime.now(polite_http.JST) - timedelta(days=retry_missing_days)).strftime("%Y%m%d")
    done = set()
    for r in log.itertuples():
        if r.status == "ok" or (r.status == "no_file" and r.race_date < recent):
            done.add((r.race_date, r.kind))   # 直近数日の「ファイルなし」は公開前かもしれないので再確認
    items = [(d, k) for d in _daterange_desc(start, end) for k in kinds if (d, k) not in done]
    print(f"[DL] 対象 {len(items)}ファイル", flush=True)
    new_rows = []
    total = {"K": 0, "B": 0}
    last_cp = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i in range(0, len(items), 12):
            if deadline and time.time() >= deadline:
                print("[DL] 時間切れで中断（次回続きから）", flush=True)
                break
            batch = items[i:i + 12]
            for rec, frames in ex.map(lambda t: _fetch_parse(*t), batch):
                for kind_name, df in frames.items():
                    store.append(kind_name, df)
                total[rec["kind"]] += int(rec["rows"] or 0)
                new_rows.append(rec)
                print(f"[DL] {rec['race_date']} {rec['kind']} {rec['status']} "
                      f"venues={rec['venues']} races={rec['races']}", flush=True)
            log = _merge_log(log, new_rows)
            new_rows = []
            if time.time() - last_cp >= checkpoint_minutes * 60:
                checkpoint(checkpoint_cmd)   # 取得済み分を途中でpush（止まっても失わない）
                last_cp = time.time()
    return total


def _merge_log(log, new_rows):
    if not new_rows:
        return log
    add = pd.DataFrame(new_rows).astype(str)
    merged = pd.concat([log, add], ignore_index=True).drop_duplicates(["race_date", "kind"], keep="last")
    save_dates_log(merged)
    return merged


def seal_finished_months(today_yyyymmdd):
    """全日付の取得が済んだ過去の月だけ圧縮する（途中の月は非圧縮のまま追記を続ける）。"""
    cur = str(today_yyyymmdd)[:6]
    log = load_dates_log()
    ok = {(r.race_date, r.kind) for r in log.itertuples() if r.status in ("ok", "no_file")}
    for kind, src in (("k_results", "K"), ("k_payouts", "K"), ("b_programs", "B")):
        for y, m in store.list_months(kind):
            if f"{y}{m}" >= cur:
                continue
            first = datetime(int(y), int(m), 1)
            nxt = (first + timedelta(days=32)).replace(day=1)
            days = [(first + timedelta(days=i)).strftime("%Y%m%d") for i in range((nxt - first).days)]
            if all((dd, src) in ok for dd in days) and store.seal(kind, y, m):
                print(f"[DL] 圧縮 {kind} {y}-{m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default="")
    ap.add_argument("--kinds", default="K,B")
    ap.add_argument("--max-minutes", type=float, default=0)
    ap.add_argument("--stop-at", default="", help="この時刻(JST HH:MM)で止める")
    ap.add_argument("--checkpoint-cmd", default="", help="途中保存コマンド（例: bash tools/commit_history_data.sh）")
    ap.add_argument("--checkpoint-minutes", type=float, default=30)
    ap.add_argument("--debug", default="", help="kind,date,jcd の生テキストを表示して終了")
    args = ap.parse_args()
    if args.debug:
        kind, d, j = args.debug.split(",")
        debug_block(kind, d, j)
        return
    end = args.end or (polite_http.now_jst() - timedelta(days=1)).strftime("%Y%m%d")
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    if args.stop_at:
        t = polite_http.deadline_from_stop_at(args.stop_at)
        deadline = min(deadline, t) if deadline else t
    total = run(args.start, end, kinds=tuple(k.strip() for k in args.kinds.split(",") if k.strip()),
                deadline=deadline, checkpoint_cmd=args.checkpoint_cmd,
                checkpoint_minutes=args.checkpoint_minutes)
    seal_finished_months(polite_http.now_jst().strftime("%Y%m%d"))
    print(f"[DL] 終了 K={total['K']}行 B={total['B']}行 リクエスト{polite_http.request_count()}回", flush=True)


if __name__ == "__main__":
    main()
