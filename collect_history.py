"""
学習用の過去データ収集スクリプト。

目的:
  現在の学習データ(sample_history.csv)には出走表の5項目
  （選手勝率・当地勝率・モーター2連対率・ボート2連対率・平均ST）
  しか入っておらず、展示タイム・級別・天候はモデルが一度も
  学習したことがない。アプリではこれらを「手で決めた重みの
  ランク補正」として後付けしているだけで、重みを推定していない。

  検証435レースのアブレーションでは、補正を全部足しても
  本命的中率は 59.2% → 59.9% とほぼ動かなかった一方、
  実際に1着だった艇へ与えた確率は 39.8% → 44.3% と
  4.5ポイント上がっていた。情報は入っているが、固定の重みでは
  引き出しきれていない可能性がある。

  そこで展示・級別・天候を含む学習データを作り直し、
  重みをモデル自身に推定させて精度が伸びるかを検証する。

使い方:
  python collect_history.py --start 20260301 --end 20260831 --out history_full.csv

  中断しても再開できる。既にoutに入っているレースは自動でスキップする。
  公式サイトへの負荷を避けるため、リクエスト間に待機を入れている。
  1レースあたり2リクエスト＋待機で、おおよそ 1,000レース/時間 が目安。
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

import re

import requests
from bs4 import BeautifulSoup

from official_fetcher import VENUES, UA, fetch_racelist, fetch_beforeinfo, fetch_race_result


# 公式サイトへの負荷を抑えるための待機（秒）。
SLEEP_MIN = 0.4
SLEEP_MAX = 0.7

# 1レースで何回まで再試行するか。
# 非開催や未確定は再試行しても無駄なので、1回で見切る。
RETRY = 1

# 開催日一覧のURL（その日どの会場が開催しているかを1リクエストで調べる）
INDEX_URL = "https://www.boatrace.jp/owpc/pc/race/index"


def fetch_open_venues(date_yyyymmdd):
    """
    その日に開催している会場コードの集合を返す。

    これを使わないと、非開催の会場にも1R・2Rを試しに行くことになり、
    24会場×2レース＝48回の空振りが毎日発生する。実測では取得より
    空振りの方が時間を食っていた（160レース/1.5時間）。
    日ごとに1リクエスト増えるだけで、この無駄がほぼ消える。

    取得に失敗した場合は None を返し、呼び出し側は従来どおり
    全会場を試す（安全側に倒す）。
    """
    try:
        r = requests.get(
            f"{INDEX_URL}?hd={date_yyyymmdd}", headers=UA, timeout=20,
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "lxml")
    codes = set()
    for a in soup.find_all("a", href=re.compile(r"jcd=(\d{2})")):
        m = re.search(r"jcd=(\d{2})", a.get("href", ""))
        if m and m.group(1) in VENUES:
            codes.add(m.group(1))
    return codes or None

# 何レースごとに途中保存するか。中断に備えてこまめに書く。
FLUSH_EVERY = 20

# 同時に取得するレース数。
#
# 実測では1レースあたり34秒かかっていた（1レース3リクエスト、
# 1リクエストの往復が10秒前後）。待機時間を削っても数%しか変わらず、
# 律速はサーバーの応答待ちだった。応答待ちの間は何もしていないので、
# 並列にすればその分だけ素直に速くなる。
#
# 各ワーカーは1リクエストごとに待機を挟むため、6並列でも
# 全体で毎秒1リクエスト未満に収まる（応答が10秒なら 6/31 ≒ 0.2 req/s）。
# 公式サイトへの負荷としては控えめな水準。
WORKERS = 6


class TimeUp(Exception):
    """指定時間に達したことを知らせる内部例外。"""


def _install_deadline_alarm(seconds):
    """
    指定秒後に必ず処理を中断させる。

    ループ先頭の時刻チェックだけだと、通信が固まって戻ってこない場合に
    いつまでも判定に到達しない。SIGALRMで割り込むことで、
    どこで止まっていても確実に抜けられるようにする。
    """
    if seconds <= 0:
        return

    def _handler(signum, frame):
        raise TimeUp()

    try:
        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(int(seconds))
    except Exception:
        # Windowsなど SIGALRM が無い環境では時刻チェックだけで動く。
        pass


def _sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def _daterange(start, end):
    s = datetime.strptime(str(start), "%Y%m%d")
    e = datetime.strptime(str(end), "%Y%m%d")
    while s <= e:
        yield s.strftime("%Y%m%d")
        s += timedelta(days=1)


def _load_done(path):
    """既に取得済みのレースキーを読む（再開用）。"""
    if not os.path.exists(path):
        return set(), []
    try:
        df = pd.read_csv(path)
    except Exception:
        return set(), []
    if "race_key" not in df.columns:
        return set(), []
    return set(df["race_key"].astype(str)), [df]


def collect_race(hd, jcd, rno):
    """
    1レース分を6行（艇ごと）で返す。
    結果が未確定・欠場などで取れない場合は None。
    """
    # 結果を先に取る。取れなければ学習に使えないので、
    # 無駄な取得を避けるためここで打ち切る。
    result = fetch_race_result(hd, jcd, rno)
    _sleep()

    rl = fetch_racelist(hd, jcd, rno)
    _sleep()

    bi = fetch_beforeinfo(hd, jcd, rno)

    if rl is None or len(rl) == 0:
        return None

    df = rl.merge(bi, on="lane", how="left", suffixes=("", "_bi"))

    finish_map = {
        int(result["first"]): 1,
        int(result["second"]): 2,
        int(result["third"]): 3,
    }

    df["finish"] = df["lane"].map(finish_map).fillna(4).astype(int)
    df["race_date"] = hd
    df["jcd"] = str(jcd).zfill(2)
    df["venue"] = VENUES.get(str(jcd).zfill(2), str(jcd).zfill(2))
    df["race_no"] = int(rno)
    df["race_key"] = f"{hd}_{str(jcd).zfill(2)}_{int(rno)}"
    df["trifecta"] = result["trifecta"]
    df["trifecta_payout_per_100"] = result["trifecta_payout_per_100"]

    drop = [c for c in df.columns if c.startswith("source_") or c.endswith("_bi")]
    return df.drop(columns=drop, errors="ignore")


def _try_race(hd, jcd, rno):
    """1レース取得。(結果, エラー文字列) を返す。スレッドから呼ばれる。"""
    last = ""
    for _ in range(RETRY):
        try:
            return collect_race(hd, jcd, rno), ""
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:160]}"
            _sleep()
    return None, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="開始日 YYYYMMDD")
    ap.add_argument("--end", required=True, help="終了日 YYYYMMDD")
    ap.add_argument("--out", default="history_full.csv")
    ap.add_argument("--venues", default="", help="会場コードをカンマ区切りで指定（既定は全24場）")
    ap.add_argument("--max-races", type=int, default=0, help="この件数に達したら終了（0で無制限）")
    ap.add_argument("--workers", type=int, default=WORKERS, help="同時取得数")
    ap.add_argument(
        "--max-hours", type=float, default=0,
        help="この時間で打ち切る（GitHub Actionsの6時間制限に引っかかる前に終わらせる用）",
    )
    args = ap.parse_args()

    globals()['WORKERS'] = max(1, int(args.workers))

    deadline = time.time() + args.max_hours * 3600 if args.max_hours else None
    if args.max_hours:
        # 通信が固まってもここで確実に打ち切る。
        _install_deadline_alarm(args.max_hours * 3600)

    codes = (
        [v.strip().zfill(2) for v in args.venues.split(",") if v.strip()]
        if args.venues
        else sorted(VENUES.keys())
    )

    done, frames = _load_done(args.out)
    print(f"[COLLECT] 取得済み {len(done)}レース / 対象会場 {len(codes)}場", flush=True)

    buf = []
    ok = 0
    skip = 0
    attempts = 0
    errors_shown = 0
    last_report = time.time()
    started = time.time()

    # 最初に1レースだけ試して、そもそも取得できるかを確かめる。
    # ここで失敗すれば以降も全部失敗するので、原因を出して即終了する。
    print("[COLLECT] 接続テスト中...", flush=True)
    probe_ok = False
    for hd in _daterange(args.start, args.end):
        for jcd in codes:
            try:
                t = collect_race(hd, jcd, 1)
                if t is not None and len(t):
                    print(
                        f"[COLLECT] 接続OK ({hd} {VENUES.get(jcd, jcd)} 1R, {len(t)}行, "
                        f"列数{len(t.columns)})",
                        flush=True,
                    )
                    probe_ok = True
            except Exception as e:
                print(
                    f"[COLLECT] テスト失敗 {hd} {VENUES.get(jcd, jcd)} 1R -> "
                    f"{type(e).__name__}: {str(e)[:200]}",
                    flush=True,
                )
            break
        break
    if not probe_ok:
        print(
            "[COLLECT] 最初の1レースが取得できませんでした。"
            "その日その場が非開催なら正常なので続行しますが、"
            "以降も失敗が続く場合は上のエラー内容を確認してください。",
            flush=True,
        )

    try:
      for hd in _daterange(args.start, args.end):
        # その日の開催会場を1リクエストで調べ、非開催の会場は飛ばす。
        open_codes = fetch_open_venues(hd)
        if open_codes is None:
            day_codes = codes                      # 取れなければ従来どおり全会場
        else:
            day_codes = [c for c in codes if c in open_codes]
            if not day_codes:
                print(f"[COLLECT] {hd} 開催なし", flush=True)
                continue
        _sleep()

        for jcd in day_codes:
            targets = [
                rno for rno in range(1, 13)
                if f"{hd}_{jcd}_{rno}" not in done
            ]
            if not targets:
                continue

            if deadline and time.time() >= deadline:
                raise TimeUp()

            # 1会場12レースをまとめて並列取得する。
            # 応答待ちが律速なので、ここを並列にするのが一番効く。
            results = {}
            errs = {}
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(_try_race, hd, jcd, r): r for r in targets}
                for fu in as_completed(futs):
                    r = futs[fu]
                    try:
                        got, err = fu.result()
                    except Exception as e:
                        got, err = None, f"{type(e).__name__}: {str(e)[:160]}"
                    results[r] = got
                    if err:
                        errs[r] = err

            for r in sorted(results):
                attempts += 1
                got = results[r]
                if got is None or len(got) == 0:
                    skip += 1
                    err = errs.get(r, "")
                    if err and (errors_shown < 5 or attempts % 200 == 0):
                        print(
                            f"[COLLECT] 失敗 {hd} {VENUES.get(jcd, jcd)} {r}R -> {err}",
                            flush=True,
                        )
                        errors_shown += 1
                    continue
                buf.append(got)
                done.add(f"{hd}_{jcd}_{r}")
                ok += 1

            if len(buf) >= FLUSH_EVERY:
                _flush(buf, frames, args.out)
                buf = []

            if time.time() - last_report >= 60:
                last_report = time.time()
                el = time.time() - started
                print(
                    f"[COLLECT] {hd} {VENUES.get(jcd, jcd)}  "
                    f"試行{attempts} 取得{ok} スキップ{skip} "
                    f"({ok / max(el / 3600, 1e-9):.0f}レース/時)",
                    flush=True,
                )

            if args.max_races and ok >= args.max_races:
                _flush(buf, frames, args.out)
                print(f"[COLLECT] 指定件数に到達。取得{ok}件", flush=True)
                return

    except TimeUp:
        pass

    try:
        signal.alarm(0)
    except Exception:
        pass

    _flush(buf, frames, args.out)
    print(f"[COLLECT] 終了 取得{ok} スキップ{skip} -> {args.out}", flush=True)
    print("[COLLECT] 続きを集めるには同じ条件でもう一度実行してください。", flush=True)


def _flush(buf, frames, path):
    if not buf:
        return
    new = pd.concat(buf, ignore_index=True)
    if os.path.exists(path):
        new.to_csv(path, mode="a", header=False, index=False, encoding="utf-8-sig")
    else:
        new.to_csv(path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
