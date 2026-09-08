"""既存 history_full.csv に公式結果の決まり手を追記する。"""

from __future__ import annotations

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from official_fetcher import fetch_race_result


def _fetch_one(race_key, hd, jcd, rno):
    try:
        result = fetch_race_result(str(hd), str(jcd).zfill(2), int(rno))
        return race_key, result.get("kimarite"), ""
    except Exception as e:
        return race_key, None, f"{type(e).__name__}: {str(e)[:160]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="history_full.csv")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-races", type=int, default=0)
    ap.add_argument("--flush-every", type=int, default=100)
    args = ap.parse_args()

    df = pd.read_csv(args.path, dtype={"jcd": str})
    if "kimarite" not in df.columns:
        df["kimarite"] = pd.NA

    needed = (
        df[df["kimarite"].isna() | df["kimarite"].astype(str).str.strip().eq("")]
        [["race_key", "race_date", "jcd", "race_no"]]
        .drop_duplicates("race_key")
        .copy()
    )

    if args.max_races > 0:
        needed = needed.head(int(args.max_races))

    print(f"[BACKFILL KIMARITE] target={len(needed)}", flush=True)
    if len(needed) == 0:
        return

    workers = max(1, min(int(args.workers), 6))
    completed = 0
    updated = 0
    errors = 0

    rows = needed.to_dict("records")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _fetch_one,
                str(row["race_key"]),
                str(row["race_date"]),
                str(row["jcd"]).zfill(2),
                int(row["race_no"]),
            ): str(row["race_key"])
            for row in rows
        }

        for future in as_completed(futures):
            race_key, kimarite, err = future.result()
            completed += 1

            if kimarite:
                mask = df["race_key"].astype(str).eq(str(race_key))
                df.loc[mask, "kimarite"] = str(kimarite)
                updated += 1
            else:
                errors += 1
                if errors <= 20:
                    print(f"[BACKFILL KIMARITE ERROR] {race_key} {err}", flush=True)

            if completed % max(1, int(args.flush_every)) == 0:
                df.to_csv(args.path, index=False, encoding="utf-8-sig")
                print(
                    f"[BACKFILL KIMARITE] {completed}/{len(rows)} "
                    f"updated={updated} errors={errors}",
                    flush=True,
                )
                time.sleep(random.uniform(0.2, 0.5))

    df.to_csv(args.path, index=False, encoding="utf-8-sig")
    print(
        f"[BACKFILL KIMARITE DONE] updated={updated} errors={errors}",
        flush=True,
    )


if __name__ == "__main__":
    main()
