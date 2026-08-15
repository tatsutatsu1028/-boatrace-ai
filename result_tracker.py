
from __future__ import annotations

from pathlib import Path
import json
import math
from datetime import datetime

import numpy as np
import pandas as pd


RESULTS_FILE = Path(__file__).parent / "prediction_results.csv"


RESULT_COLUMNS = [
    "saved_at",
    "race_date",
    "venue",
    "race_no",
    "race_key",
    "first_actual",
    "second_actual",
    "third_actual",
    "trifecta_actual",
    "p1_lane",
    "p1_prob",
    "top_ticket",
    "top_ticket_prob",
    "top_ticket_odds",
    "top_ticket_stake",
    "total_stake",
    "payout",
    "profit",
    "roi",
    "hit_top_ticket",
    "hit_any_ticket",
    "predicted_first_hit",
    "tickets_json",
]


def _safe_float(v, default=np.nan):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _load_raw():
    if RESULTS_FILE.exists():
        try:
            df = pd.read_csv(RESULTS_FILE)
        except Exception:
            df = pd.DataFrame(columns=RESULT_COLUMNS)
    else:
        df = pd.DataFrame(columns=RESULT_COLUMNS)

    for c in RESULT_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df[RESULT_COLUMNS]


def load_results():
    return _load_raw()


def _write(df):
    df.to_csv(RESULTS_FILE, index=False, encoding="utf-8-sig")


def result_exists(race_key):
    df = _load_raw()
    if len(df) == 0:
        return False
    return (df["race_key"].astype(str) == str(race_key)).any()


def save_race_result(
    race_key,
    race_date,
    venue,
    race_no,
    final,
    tickets,
    first_actual,
    second_actual,
    third_actual,
    payout=0,
):
    """
    AI予想と実着順を1レース分保存。
    payout は購入した買い目全体に対する実受取額（円）。
    """

    final = final.copy()
    tickets = tickets.copy()

    if "p_first" not in final.columns:
        raise ValueError("final に p_first 列がありません。")

    actual_combo = f"{int(first_actual)}-{int(second_actual)}-{int(third_actual)}"

    ranked_first = final.sort_values("p_first", ascending=False).reset_index(drop=True)
    p1_lane = _safe_int(ranked_first.iloc[0]["lane"])
    p1_prob = _safe_float(ranked_first.iloc[0]["p_first"], 0.0)

    ranked_tickets = tickets.sort_values(
        ["prob", "expected_return"] if "expected_return" in tickets.columns else ["prob"],
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    if len(ranked_tickets):
        top_ticket = str(ranked_tickets.iloc[0].get("combo", ""))
        top_ticket_prob = _safe_float(ranked_tickets.iloc[0].get("prob"), 0.0)
        top_ticket_odds = _safe_float(ranked_tickets.iloc[0].get("odds"), np.nan)
        top_ticket_stake = _safe_int(ranked_tickets.iloc[0].get("stake"), 0)
    else:
        top_ticket = ""
        top_ticket_prob = 0.0
        top_ticket_odds = np.nan
        top_ticket_stake = 0

    stake_series = pd.to_numeric(tickets.get("stake", 0), errors="coerce")
    if not isinstance(stake_series, pd.Series):
        total_stake = _safe_int(stake_series, 0)
    else:
        total_stake = int(stake_series.fillna(0).sum())

    purchased = tickets.copy()
    if "stake" in purchased.columns:
        purchased = purchased[pd.to_numeric(purchased["stake"], errors="coerce").fillna(0) > 0]

    hit_any_ticket = actual_combo in set(purchased.get("combo", pd.Series(dtype=str)).astype(str))
    hit_top_ticket = actual_combo == top_ticket
    predicted_first_hit = int(first_actual) == p1_lane

    payout = _safe_int(payout, 0)
    profit = payout - total_stake
    roi = (payout / total_stake) if total_stake > 0 else np.nan

    keep_cols = ["combo", "group", "prob", "odds", "expected_return", "stake"]
    ticket_payload = []
    for _, row in purchased.iterrows():
        item = {}
        for c in keep_cols:
            if c in row.index:
                val = row[c]
                if pd.isna(val):
                    item[c] = None
                elif isinstance(val, (np.integer,)):
                    item[c] = int(val)
                elif isinstance(val, (np.floating,)):
                    item[c] = float(val)
                else:
                    item[c] = val
        ticket_payload.append(item)

    record = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "race_date": str(race_date),
        "venue": str(venue),
        "race_no": int(race_no),
        "race_key": str(race_key),
        "first_actual": int(first_actual),
        "second_actual": int(second_actual),
        "third_actual": int(third_actual),
        "trifecta_actual": actual_combo,
        "p1_lane": p1_lane,
        "p1_prob": p1_prob,
        "top_ticket": top_ticket,
        "top_ticket_prob": top_ticket_prob,
        "top_ticket_odds": top_ticket_odds,
        "top_ticket_stake": top_ticket_stake,
        "total_stake": total_stake,
        "payout": payout,
        "profit": profit,
        "roi": roi,
        "hit_top_ticket": bool(hit_top_ticket),
        "hit_any_ticket": bool(hit_any_ticket),
        "predicted_first_hit": bool(predicted_first_hit),
        "tickets_json": json.dumps(ticket_payload, ensure_ascii=False),
    }

    df = _load_raw()
    df = df[df["race_key"].astype(str) != str(race_key)]
    df = pd.concat([df, pd.DataFrame([record])], ignore_index=True)
    _write(df)

    return record


def delete_result(race_key):
    df = _load_raw()
    before = len(df)
    df = df[df["race_key"].astype(str) != str(race_key)]
    _write(df)
    return before - len(df)


def metrics(df=None):
    if df is None:
        df = _load_raw()

    if len(df) == 0:
        return {
            "races": 0,
            "first_hit_rate": np.nan,
            "ticket_hit_rate": np.nan,
            "top_ticket_hit_rate": np.nan,
            "total_stake": 0,
            "total_payout": 0,
            "profit": 0,
            "roi": np.nan,
            "brier_first": np.nan,
        }

    first_hit = df["predicted_first_hit"].astype(str).str.lower().isin(["true", "1"])
    any_hit = df["hit_any_ticket"].astype(str).str.lower().isin(["true", "1"])
    top_hit = df["hit_top_ticket"].astype(str).str.lower().isin(["true", "1"])

    total_stake = pd.to_numeric(df["total_stake"], errors="coerce").fillna(0).sum()
    total_payout = pd.to_numeric(df["payout"], errors="coerce").fillna(0).sum()

    # 1着トップ予想の簡易Brier score:
    # predicted top lane が当たりなら y=1、外れなら0
    p = pd.to_numeric(df["p1_prob"], errors="coerce")
    y = first_hit.astype(float)
    valid = p.notna()
    brier = float(((p[valid] - y[valid]) ** 2).mean()) if valid.any() else np.nan

    return {
        "races": int(len(df)),
        "first_hit_rate": float(first_hit.mean()),
        "ticket_hit_rate": float(any_hit.mean()),
        "top_ticket_hit_rate": float(top_hit.mean()),
        "total_stake": int(total_stake),
        "total_payout": int(total_payout),
        "profit": int(total_payout - total_stake),
        "roi": float(total_payout / total_stake) if total_stake > 0 else np.nan,
        "brier_first": brier,
    }


def calibration_table(df=None):
    if df is None:
        df = _load_raw()
    if len(df) == 0:
        return pd.DataFrame(columns=["予測帯", "件数", "平均予測確率", "実的中率"])

    x = df.copy()
    x["p1_prob"] = pd.to_numeric(x["p1_prob"], errors="coerce")
    x["hit"] = x["predicted_first_hit"].astype(str).str.lower().isin(["true", "1"]).astype(float)
    x = x.dropna(subset=["p1_prob"])
    if len(x) == 0:
        return pd.DataFrame(columns=["予測帯", "件数", "平均予測確率", "実的中率"])

    bins = [0, .2, .4, .6, .8, 1.000001]
    labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]
    x["予測帯"] = pd.cut(x["p1_prob"], bins=bins, labels=labels, include_lowest=True, right=False)

    g = x.groupby("予測帯", observed=False).agg(
        件数=("hit", "size"),
        平均予測確率=("p1_prob", "mean"),
        実的中率=("hit", "mean"),
    ).reset_index()

    g["平均予測確率"] = (g["平均予測確率"] * 100).round(1)
    g["実的中率"] = (g["実的中率"] * 100).round(1)
    return g
