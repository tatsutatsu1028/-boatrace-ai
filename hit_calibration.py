"""
「この買い目の的中確率」の表示補正。

画面の的中確率は各買い目の3連単確率の合計（stake_allocator.ticket_hit_probability）。
順番（高いレースほど当たる）は正しいが、値そのものは実際の的中率より低く出る
（事後予想 7,701レースで表示の平均26%に対して実際45%）。そこで全レースの事後予想
（hindcast_predictions）の「表示値 → 候補内的中」から等張回帰（単調な変換）を作り、
表示する数字だけを実績に合わせる。買い目の選び方・資金配分には使わない。

  - 補正は hit_probability_calibrations に1回1行で保存する（変換表 knots と、
    作った時点での帯別の当てはまり・時系列の検証）
  - hindcast.yml の最後に `python hit_calibration.py` を流し、今の予想ロジックの版の
    補正が無いか、最後に作ってから REBUILD_DAYS 日たっていれば作り直す（週1回）
  - 補正前（hit_probability）と補正後（hit_probability_calibrated）、使った補正の id を
    固定予想・検証結果・事後予想の各行に残し、補正後の値が実際の的中率と合っているかを
    後から確かめられるようにする

使い方:
  python hit_calibration.py            # 必要なら作り直す
  python hit_calibration.py --force    # 必ず作り直す
  python hit_calibration.py --dry-run  # 作るだけで保存しない
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

CALIBRATION_TABLE = "hit_probability_calibrations"
HINDCAST_TABLE = "hindcast_predictions"
METHOD = "isotonic"

# 補正を作り直す間隔（日）
REBUILD_DAYS = 7
# これより少ない件数では補正を作らない（表示は補正前のまま）
MIN_SAMPLES = 1000
# 等張回帰の1段あたりの最低件数。端の数件だけで 0% や 100% にならないよう、
# これより少ない段は隣の段とまとめる。
MIN_BLOCK = 200
# 時系列の検証で後ろに取っておく割合（日付の新しい側）
HOLDOUT_FRACTION = 0.2


def prediction_version():
    """予想ロジックの版（hindcast_predictions.model_version と同じ文字列）。

    1着〜3着モデル・買い目方針のどれかが変わればこの文字列も変わり、補正も作り直す。
    結果の取り込み（auto_result_collect / track_odds）は予想モデルを読み込まないので、
    prediction はここで初めて読む。
    """
    from prediction import MODEL_VERSION, SECOND_FAVORITE_POLICY

    return f"{MODEL_VERSION}+{SECOND_FAVORITE_POLICY}"


BAND_EDGES = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.000001]
BAND_LABELS = ["20%未満", "20-30%", "30-40%", "40-50%", "50-60%", "60-70%", "70%以上"]


# ---------------------------------------------------------------
# 補正の作成と適用
# ---------------------------------------------------------------
def fit_isotonic(raw, hit, min_block=None):
    """表示値 raw（0〜1）と的中 hit（0/1）から単調非減少の変換表を作る。

    重み付きの pool-adjacent-violators で段を作り、件数が min_block 未満の段は
    隣とまとめる。各段の「表示値の平均 → 的中率」を節点として返し、
    節点の間は直線でつなぐ（apply_calibration）。
    """
    min_block = MIN_BLOCK if min_block is None else min_block
    raw = np.asarray(raw, dtype=float)
    hit = np.asarray(hit, dtype=float)
    ok = np.isfinite(raw) & np.isfinite(hit)
    raw, hit = raw[ok], hit[ok]
    if len(raw) == 0:
        raise ValueError("補正に使えるデータがありません。")

    xs, inverse = np.unique(raw, return_inverse=True)
    weights = np.bincount(inverse).astype(float)
    hit_sum = np.bincount(inverse, weights=hit)
    raw_sum = xs * weights

    # 段: [件数, 的中数, 表示値の合計]
    blocks = []
    for w, h, r in zip(weights, hit_sum, raw_sum):
        blocks.append([w, h, r])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][0] >= blocks[-1][1] / blocks[-1][0]:
            w2, h2, r2 = blocks.pop()
            blocks[-1] = [blocks[-1][0] + w2, blocks[-1][1] + h2, blocks[-1][2] + r2]

    # 件数の少ない段を隣とまとめる（単調な隣どうしの平均なので単調性は保たれる）
    i = 0
    while len(blocks) > 1 and i < len(blocks):
        if blocks[i][0] >= min_block:
            i += 1
            continue
        j = i + 1 if i + 1 < len(blocks) else i - 1
        lo, hi = min(i, j), max(i, j)
        merged = [a + b for a, b in zip(blocks[lo], blocks[hi])]
        blocks[lo:hi + 1] = [merged]
        i = lo

    x = [b[2] / b[0] for b in blocks]
    y = [b[1] / b[0] for b in blocks]
    return {
        "x": [round(float(v), 6) for v in x],
        "y": [round(float(v), 6) for v in y],
        "n": [int(b[0]) for b in blocks],
    }


def apply_calibration(raw, calibration):
    """表示値 raw（0〜1）を補正後の値にする。補正が無い・値が読めない場合は None。"""
    if raw is None or not calibration:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    knots = calibration.get("knots") or {}
    x, y = knots.get("x") or [], knots.get("y") or []
    if not x or len(x) != len(y):
        return None
    # 節点の外側は端の値のまま（外へ延ばさない）
    return float(min(max(np.interp(v, x, y), 0.0), 1.0))


def calibrated_hit_fields(raw, calibration):
    """固定予想・事後予想に残す補正後の値と、使った補正の id。"""
    value = apply_calibration(raw, calibration)
    if value is None:
        return {"hit_probability_calibrated": None, "hit_calibration_id": None}
    return {
        "hit_probability_calibrated": round(value, 6),
        "hit_calibration_id": calibration.get("id"),
    }


# 検証結果（prediction_results）の補正の列。マイグレーション
# （20261006000400_create_hit_probability_calibrations.sql）前の DB には無いため、
# 値があるときだけ送る。
CALIBRATED_RESULT_COLUMNS = ("hit_probability_calibrated", "hit_calibration_id")


def snapshot_calibrated_fields(payload):
    """固定予想の payload に残した補正後の値と補正の id。無ければ空の dict。"""
    if not isinstance(payload, dict):
        return {}
    try:
        value = float(payload.get("hit_probability_calibrated"))
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(value):
        return {}
    cal_id = payload.get("hit_calibration_id")
    try:
        cal_id = int(cal_id) if cal_id is not None else None
    except (TypeError, ValueError):
        cal_id = None
    return {"hit_probability_calibrated": value, "hit_calibration_id": cal_id}


def band_table(prob, hit, calibrated=None):
    """表示値の帯ごとの件数・平均表示値・（補正後の平均）・実際の的中率。"""
    prob = np.asarray(prob, dtype=float)
    hit = np.asarray(hit, dtype=float)
    cal = None if calibrated is None else np.asarray(calibrated, dtype=float)
    rows = []
    for label, lo, hi in zip(BAND_LABELS, BAND_EDGES, BAND_EDGES[1:]):
        m = (prob >= lo) & (prob < hi)
        if not m.any():
            continue
        row = {
            "band": label,
            "count": int(m.sum()),
            "mean_prob": round(float(prob[m].mean()), 4),
            "hit_rate": round(float(hit[m].mean()), 4),
        }
        if cal is not None:
            row["mean_calibrated"] = round(float(cal[m].mean()), 4)
        rows.append(row)
    return rows


def _brier(p, hit):
    return round(float(np.mean((np.asarray(p, float) - np.asarray(hit, float)) ** 2)), 5)


def holdout_check(dates, raw, hit, fraction=HOLDOUT_FRACTION):
    """日付の古い側で作った補正が、新しい側でも実績に合うかを確かめる。"""
    dates = np.asarray(dates)
    order = np.sort(np.unique(dates))
    if len(order) < 5:
        return None
    cut = order[int(len(order) * (1 - fraction))]
    train = dates < cut
    test = ~train
    if train.sum() < MIN_SAMPLES or test.sum() == 0:
        return None
    knots = fit_isotonic(raw[train], hit[train])
    cal = np.array([apply_calibration(v, {"knots": knots}) for v in raw[test]])
    return {
        "train_until": str(order[order < cut][-1]),
        "test_from": str(cut),
        "train_count": int(train.sum()),
        "test_count": int(test.sum()),
        "test_hit_rate": round(float(hit[test].mean()), 4),
        "test_mean_raw": round(float(raw[test].mean()), 4),
        "test_mean_calibrated": round(float(cal.mean()), 4),
        "brier_raw": _brier(raw[test], hit[test]),
        "brier_calibrated": _brier(cal, hit[test]),
        "bands_by_calibrated": band_table(cal, hit[test]),
    }


def build_calibration(dates, raw, hit):
    """hit_probability_calibrations に保存する1行（id・作成日時を除く）。"""
    dates = np.asarray(dates)
    raw = np.asarray(raw, dtype=float)
    hit = np.asarray(hit, dtype=float)
    knots = fit_isotonic(raw, hit)
    cal = np.array([apply_calibration(v, {"knots": knots}) for v in raw])
    return {
        "model_version": prediction_version(),
        "method": METHOD,
        "sample_count": int(len(raw)),
        "data_start": str(min(dates)),
        "data_end": str(max(dates)),
        "hit_rate": round(float(hit.mean()), 4),
        "raw_mean": round(float(raw.mean()), 4),
        "knots": knots,
        "bands": band_table(raw, hit, cal),
        "holdout": holdout_check(dates, raw, hit),
    }


# ---------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------
def fetch_latest(url, key, model_version=None, timeout=15):
    """その版（既定は今の版）の最新の補正（無ければ None）。アプリは Publishable key で読む。"""
    if not url or not key:
        return None
    model_version = model_version or prediction_version()
    r = requests.get(
        f"{url}/rest/v1/{CALIBRATION_TABLE}",
        params=[
            ("select", "id,created_at,model_version,method,sample_count,data_start,data_end,knots"),
            ("model_version", f"eq.{model_version}"),
            ("order", "created_at.desc"),
            ("limit", "1"),
        ],
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
    r.raise_for_status()
    rows = r.json() or []
    return rows[0] if rows else None


_ENV_CACHE = {"at": 0.0, "value": None}


def latest_from_env(ttl_seconds=3600):
    """GitHub Actions（SUPABASE_URL / SUPABASE_KEY）用。読めなければ None（表示は補正前）。"""
    if time.time() - _ENV_CACHE["at"] < ttl_seconds:
        return _ENV_CACHE["value"]
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    try:
        value = fetch_latest(url, key)
    except Exception as e:  # noqa: BLE001
        print(f"[HIT_CAL] 補正を読めないため補正前の値だけ残す: {type(e).__name__}", flush=True)
        value = None
    _ENV_CACHE.update(at=time.time(), value=value)
    return value


def _fetch_hindcast(model_version):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    rows, offset, page = [], 0, 1000
    while True:
        r = _request(
            "GET",
            f"{url}/rest/v1/{HINDCAST_TABLE}",
            params=[
                ("select", "race_key,race_date,candidate_hit_probability,candidate_hit,"
                           "candidate_hit_probability_calibrated,hit_calibration_id"),
                ("model_version", f"eq.{model_version}"),
                ("candidate_hit", "not.is.null"),
                ("candidate_hit_probability", "not.is.null"),
                ("order", "race_key.asc"),
                ("limit", str(page)),
                ("offset", str(offset)),
            ],
            headers=_headers(),
            timeout=60,
        )
        chunk = r.json() or []
        rows.extend(chunk)
        if len(chunk) < page:
            return rows
        offset += page


def previous_check(rows):
    """事後予想を保存した時点の補正（それより前のデータで作ったもの）での当てはまり。"""
    used = [r for r in rows if r.get("candidate_hit_probability_calibrated") is not None]
    if not used:
        return None
    cal = np.array([float(r["candidate_hit_probability_calibrated"]) for r in used])
    raw = np.array([float(r["candidate_hit_probability"]) for r in used])
    hit = np.array([1.0 if r["candidate_hit"] else 0.0 for r in used])
    return {
        "count": int(len(used)),
        "data_start": min(str(r["race_date"]) for r in used),
        "data_end": max(str(r["race_date"]) for r in used),
        "hit_rate": round(float(hit.mean()), 4),
        "mean_raw": round(float(raw.mean()), 4),
        "mean_calibrated": round(float(cal.mean()), 4),
        "brier_raw": _brier(raw, hit),
        "brier_calibrated": _brier(cal, hit),
        "bands_by_calibrated": band_table(cal, hit),
    }


def _needs_rebuild(latest, force):
    if force or not latest:
        return True
    created = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created >= timedelta(days=REBUILD_DAYS) - timedelta(hours=6)


def _code_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            sha = ""
    return sha[:12]


def main():
    from auto_random_fix import _cfg, _headers, _request

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="前回から日が浅くても作り直す")
    ap.add_argument("--dry-run", action="store_true", help="作るだけで保存しない")
    args = ap.parse_args()

    url, key = _cfg()
    latest = fetch_latest(url, key)
    if not _needs_rebuild(latest, args.force):
        print(f"[HIT_CAL] 前回の補正（id={latest['id']}, {latest['created_at']}）から"
              f"{REBUILD_DAYS}日たっていないため作り直さない", flush=True)
        return

    version = prediction_version()
    rows = _fetch_hindcast(version)
    if len(rows) < MIN_SAMPLES:
        print(f"[HIT_CAL] 事後予想が {len(rows)}件で {MIN_SAMPLES}件に満たないため作らない", flush=True)
        return

    dates = np.array([str(r["race_date"]) for r in rows])
    raw = np.array([float(r["candidate_hit_probability"]) for r in rows])
    hit = np.array([1.0 if r["candidate_hit"] else 0.0 for r in rows])
    row = build_calibration(dates, raw, hit)
    row["previous_check"] = previous_check(rows)
    row["code_sha"] = _code_sha()

    print(f"[HIT_CAL] 版 {version} / {row['sample_count']}レース "
          f"{row['data_start']}〜{row['data_end']} / 表示平均 {row['raw_mean']:.3f} "
          f"実際 {row['hit_rate']:.3f} / 段 {len(row['knots']['x'])}", flush=True)
    for b in row["bands"]:
        print(f"[HIT_CAL]   {b['band']}: {b['count']}件 表示 {b['mean_prob']:.3f} "
              f"補正後 {b['mean_calibrated']:.3f} 実際 {b['hit_rate']:.3f}", flush=True)
    h = row["holdout"]
    if h:
        print(f"[HIT_CAL] 時系列検証（{h['test_from']}〜 {h['test_count']}件）: 実際 {h['test_hit_rate']:.3f} "
              f"補正前 {h['test_mean_raw']:.3f} 補正後 {h['test_mean_calibrated']:.3f} / "
              f"Brier {h['brier_raw']} → {h['brier_calibrated']}", flush=True)
    p = row["previous_check"]
    if p:
        print(f"[HIT_CAL] 保存時の補正での確認（{p['count']}件）: 実際 {p['hit_rate']:.3f} "
              f"補正後 {p['mean_calibrated']:.3f}", flush=True)

    if args.dry_run:
        return
    _request(
        "POST",
        f"{url}/rest/v1/{CALIBRATION_TABLE}",
        headers=_headers("return=minimal"),
        json=row,
        timeout=30,
    )
    print("[HIT_CAL] 保存しました", flush=True)


if __name__ == "__main__":
    main()
