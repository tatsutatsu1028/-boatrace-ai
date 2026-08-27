from __future__ import annotations
import requests
from track_odds import (
    SUPABASE_URL, RESULT_TABLE, _headers, _require_config,
    _load_prediction_snapshot, _upsert_research_rules,
    fetch_race_result, _normalize_race_date,
)

def main():
    _require_config()
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{RESULT_TABLE}"
        "?select=race_key,race_date,race_no"
        "&order=race_date.asc,race_no.asc"
    )
    r = requests.get(endpoint, headers=_headers(), timeout=30)
    r.raise_for_status()
    rows = r.json() or []
    print("[BACKFILL] rows=", len(rows))

    ok = 0
    skip = 0
    for row in rows:
        ctx = str(row.get("race_key", ""))
        parts = ctx.split("_")
        if len(parts) < 3:
            skip += 1
            continue
        race_date, jcd, rno = parts[0], parts[1], int(parts[2])
        snap = _load_prediction_snapshot(ctx)
        if not snap:
            skip += 1
            continue
        try:
            official = fetch_race_result(_normalize_race_date(race_date), jcd, rno)
            _upsert_research_rules(snap, race_date, jcd, rno, official)
            ok += 1
        except Exception as e:
            print("[BACKFILL] skip", ctx, type(e).__name__, str(e))
            skip += 1

    print("[BACKFILL] done ok=", ok, "skip=", skip)

if __name__ == "__main__":
    main()
