"""
過去のレースの3連単の最終オッズ（全120通り）を公式サイトから取り、データ用リポジトリに保存する（検証用）。

万舟チャンスの「オッズ100倍以上から選ぶ買い目」の検証（tools/manshu_eval.py odds）に使う。
取るのは --targets の CSV（列 race_key）に並んだレースだけ。取得間隔は polite_http（全体で3.4秒以上）。
過去データの収集（History Backfill・Collect History）が動いていれば終わるまで待つ。

保存先: <BOATRACE_DATA_DIR>/data/odds3t/
  odds3t_YYYY-MM.csv   race_key, combo, odds（最終オッズ。締切前に買うときのオッズとは少し違う）
  fetched.csv          race_key, n, fetched_at（取った記録。n=0 は読めなかった・発売なし）
止まっても次回は続きから取る。ログは誰でも見られるので件数・時間だけを出す。

  BOATRACE_DATA_DIR=<データ用リポジトリ> python tools/odds_backfill.py \
      --targets <データ用リポジトリ>/reports/manshu/odds_targets.csv --max-minutes 300
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polite_http  # noqa: E402
from data_paths import data_path  # noqa: E402
from official_fetcher import BASE, parse_odds3t  # noqa: E402

OUT = "data/odds3t"


def log(*a):
    print("[ODDS]", *a, flush=True)


def _fetched(out):
    f = out / "fetched.csv"
    return set(pd.read_csv(f, dtype=str)["race_key"]) if f.exists() else set()


def _append(path, df):
    df.to_csv(path, mode="a", header=not path.exists(), index=False, encoding="utf-8")


def fetch_one(race_key):
    d, jcd, rno = race_key.split("_")
    url = f"{BASE}/odds3t?hd={d}&jcd={jcd}&rno={int(rno)}"
    r = polite_http.get(url, timeout=30)
    r.raise_for_status()
    return parse_odds3t(r.text, url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("--checkpoint-cmd", default="")
    ap.add_argument("--checkpoint-minutes", type=float, default=30)
    ap.add_argument("--wait-crawlers", action="store_true")
    args = ap.parse_args()

    out = data_path(OUT)
    out.mkdir(parents=True, exist_ok=True)
    targets = pd.read_csv(args.targets, dtype=str)["race_key"].drop_duplicates().tolist()
    done = _fetched(out)
    todo = [rk for rk in targets if rk not in done]
    log(f"対象 {len(targets)}レース・取得済み {len(targets) - len(todo)}・残り {len(todo)}")
    if args.wait_crawlers:
        from pre_picks import wait_for_crawlers

        wait_for_crawlers()

    t0 = last_cp = time.time()
    ok = ng = 0
    for i, rk in enumerate(todo):
        if time.time() - t0 > args.max_minutes * 60:
            log("時間切れ（次回は続きから）")
            break
        if polite_http.in_maintenance():
            log("公式サイトのメンテナンス時間のため止める（次回は続きから）")
            break
        try:
            o = fetch_one(rk)
        except Exception as e:  # noqa: BLE001  1レースの失敗では止めない（次回に取り直す）
            ng += 1
            log(f"取得失敗 {type(e).__name__}")
            if ng >= 20 and ok == 0:
                raise
            continue
        if len(o):
            o = o.assign(race_key=rk)[["race_key", "combo", "odds"]]
            _append(out / f"odds3t_{rk[:4]}-{rk[4:6]}.csv", o)
        _append(out / "fetched.csv", pd.DataFrame([{
            "race_key": rk, "n": len(o), "fetched_at": datetime.now(polite_http.JST).isoformat(timespec="seconds")}]))
        ok += 1
        if (i + 1) % 100 == 0:
            log(f"{i + 1}/{len(todo)} {time.time() - t0:.0f}秒")
        if args.checkpoint_cmd and time.time() - last_cp > args.checkpoint_minutes * 60:
            subprocess.run(args.checkpoint_cmd, shell=True, check=False)
            last_cp = time.time()
    log(f"取得 {ok}レース・失敗 {ng} {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
