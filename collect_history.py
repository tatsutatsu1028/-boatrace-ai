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
import time
from datetime import datetime, timedelta

import pandas as pd

from official_fetcher import VENUES, fetch_racelist, fetch_beforeinfo, fetch_race_result


# 公式サイトへの負荷を抑えるための待機（秒）。
# 短くしすぎると弾かれる可能性があるので、余裕を持たせる。
SLEEP_MIN = 0.7
SLEEP_MAX = 1.3

# 1レースで何回まで再試行するか。
RETRY = 2

# 何レースごとに途中保存するか。中断に備えてこまめに書く。
FLUSH_EVERY = 20


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="開始日 YYYYMMDD")
    ap.add_argument("--end", required=True, help="終了日 YYYYMMDD")
    ap.add_argument("--out", default="history_full.csv")
    ap.add_argument("--venues", default="", help="会場コードをカンマ区切りで指定（既定は全24場）")
    ap.add_argument("--max-races", type=int, default=0, help="この件数に達したら終了（0で無制限）")
    ap.add_argument(
        "--max-hours", type=float, default=0,
        help="この時間で打ち切る（GitHub Actionsの6時間制限に引っかかる前に終わらせる用）",
    )
    args = ap.parse_args()

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
        for jcd in codes:
            # その日その場が開催していなければ1Rで失敗するので、
            # 早めに次の会場へ移る。
            miss_in_a_row = 0

            for rno in range(1, 13):
                if deadline and time.time() >= deadline:
                    _flush(buf, frames, args.out)
                    print(
                        f"[COLLECT] 時間切れで停止。取得{ok}件。"
                        f"同じコマンドで再実行すると続きから再開します。",
                        flush=True,
                    )
                    return

                key = f"{hd}_{jcd}_{rno}"
                if key in done:
                    continue

                got = None
                last_err = ""
                for _ in range(RETRY):
                    try:
                        got = collect_race(hd, jcd, rno)
                        break
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {str(e)[:160]}"
                        _sleep()

                attempts += 1

                # 何も取れないまま進むと原因が分からないので、
                # 最初の数件と、一定間隔でエラー内容を必ず出す。
                if got is None or len(got) == 0:
                    skip += 1
                    miss_in_a_row += 1
                    if last_err and (errors_shown < 5 or attempts % 50 == 0):
                        print(
                            f"[COLLECT] 失敗 {hd} {VENUES.get(jcd, jcd)} {rno}R -> {last_err}",
                            flush=True,
                        )
                        errors_shown += 1
                    # 1R・2Rと続けて取れない日は非開催とみなす。
                    if miss_in_a_row >= 2 and rno <= 2:
                        break
                    continue

                miss_in_a_row = 0
                buf.append(got)
                done.add(key)
                ok += 1

                if len(buf) >= FLUSH_EVERY:
                    _flush(buf, frames, args.out)
                    buf = []

                # 成功・失敗にかかわらず60秒ごとに進捗を出す。
                # （成功時だけ出す作りだと、全部失敗した時に無言のまま止まる）
                if time.time() - last_report >= 60:
                    last_report = time.time()
                    el = time.time() - started
                    print(
                        f"[COLLECT] {hd} {VENUES.get(jcd, jcd)} {rno}R  "
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
