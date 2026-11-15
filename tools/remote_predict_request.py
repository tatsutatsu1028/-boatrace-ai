"""
predict_race.yml を手動で動かしたとき（request_id なし）に、その場で依頼（prediction_requests）を作る。

公式データ（出走表・展示・3連単オッズ）を取り、予想タブで表に手を加えずに押したときと同じ入力
（predict_flow.work_from_race）と、保存済みの本番設定（収集スタッフと同じ値）で依頼を作る。
mode が model 以外なら管理者として（Claude を呼ぶ）、model ならスタッフとして作る。
作った id を GitHub Actions の出力 request_id に書く（ログには id だけを出す）。

  python tools/remote_predict_request.py --date YYYYMMDD --jcd 01 --rno 12 --mode model \
      --collector-name actions_manual --github-output "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import os

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

import argparse  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VENUE_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖", "07": "蒲郡",
    "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門",
    "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松", "21": "芦屋",
    "22": "福岡", "23": "唐津", "24": "大村",
}


def build_input(date8, jcd, rno, mode):
    import frame_codec
    import predict_flow
    import predict_runtime
    from official_fetcher import fetch_odds3t, fetch_official_race
    from result_tracker import fetch_daily_schedule_supabase

    d = predict_flow.parse_date(date8)
    venue = VENUE_NAMES[jcd]
    race = fetch_official_race(date8, jcd, rno)
    try:
        odds = fetch_odds3t(date8, jcd, rno)
    except Exception:  # noqa: BLE001
        odds = None
    work = predict_flow.work_from_race(race, d, venue, rno)
    params = predict_runtime.staff_params(predict_runtime.load_settings_reused())
    # daily_schedule.race_date は YYYYMMDD（アプリの fetch_daily_schedule と同じ渡し方）
    schedule = fetch_daily_schedule_supabase(date8) or {}
    deadlines = (schedule.get(jcd) or {}).get("deadlines") or {}
    return frame_codec.encode({
        "date": d.isoformat(), "jcd": jcd, "rno": rno, "venue": venue, "deadlines": deadlines,
        "work": work, "odds": odds, "params": params,
        "claude_params": {k: params[k] for k in ("hedge_enabled", "longshot_min_prob_pct", "total_budget",
                                                  "min_bet", "value_bias")},
        "label": f"{d} {venue} {rno}R", "mode": mode,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--jcd", required=True)
    ap.add_argument("--rno", required=True, type=int)
    ap.add_argument("--mode", default="model", choices=["model", "claude", "avg"])
    ap.add_argument("--collector-name", default="actions_manual")
    ap.add_argument("--github-output", default="")
    a = ap.parse_args()
    date8 = a.date.replace("-", "")
    jcd = a.jcd.zfill(2)

    import remote_predict

    inp = build_input(date8, jcd, a.rno, a.mode)
    row = {
        "race_key": f"{date8}_{jcd}_{a.rno}", "mode": a.mode, "role": "staff" if a.mode == "model" else "admin",
        "collector_name": a.collector_name, "input": inp,
    }
    created = remote_predict._sb("POST", remote_predict.TABLE, json=row, prefer="return=representation").json()
    request_id = created[0]["id"]
    print(f"[REMOTE_PREDICT] 依頼を作りました {request_id}", flush=True)
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"request_id={request_id}\n")


if __name__ == "__main__":
    main()
