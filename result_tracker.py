from __future__ import annotations

from pathlib import Path
import json
import math
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st


# ローカル実行時のフォールバック用。
# Streamlit Cloud では Supabase を優先して使う。
RESULTS_FILE = Path(__file__).parent / "prediction_results.csv"

SUPABASE_TABLE = "prediction_results"


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


def _supabase_config():
    """
    Streamlit Secrets に以下があれば Supabase を使用。

    [supabase]
    url = "https://xxxxx.supabase.co"
    key = "..."
    """
    try:
        cfg = st.secrets.get("supabase", {})
        url = str(cfg.get("url", "")).strip().rstrip("/")
        key = str(cfg.get("key", "")).strip()
        if url and key:
            return url, key
    except Exception:
        pass

    return "", ""


def _use_supabase():
    url, key = _supabase_config()
    return bool(url and key)


def _headers(prefer=None):
    _, key = _supabase_config()

    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    if prefer:
        h["Prefer"] = prefer

    return h


def _json_safe(v):
    if v is None:
        return None

    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    if isinstance(v, (np.integer,)):
        return int(v)

    if isinstance(v, (np.floating,)):
        x = float(v)
        return x if math.isfinite(x) else None

    if isinstance(v, (np.bool_,)):
        return bool(v)

    return v


def _normalize_df(df):
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    out = df.copy()

    for c in RESULT_COLUMNS:
        if c not in out.columns:
            out[c] = np.nan

    return out[RESULT_COLUMNS]


# -----------------------------
# Supabase
# -----------------------------

def _load_supabase():
    url, _ = _supabase_config()

    endpoint = (
        f"{url}/rest/v1/{SUPABASE_TABLE}"
        "?select=*"
        "&order=race_date.desc,venue.asc,race_no.desc"
    )

    r = requests.get(
        endpoint,
        headers=_headers(),
        timeout=20,
    )
    r.raise_for_status()

    data = r.json()

    if not data:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    return _normalize_df(pd.DataFrame(data))


def _upsert_supabase(record):
    url, _ = _supabase_config()

    endpoint = (
        f"{url}/rest/v1/{SUPABASE_TABLE}"
        "?on_conflict=race_key"
    )

    payload = {
        k: _json_safe(record.get(k))
        for k in RESULT_COLUMNS
    }

    r = requests.post(
        endpoint,
        headers=_headers(
            "resolution=merge-duplicates,return=minimal"
        ),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def _delete_supabase(race_key):
    url, _ = _supabase_config()

    endpoint = (
        f"{url}/rest/v1/{SUPABASE_TABLE}"
        f"?race_key=eq.{requests.utils.quote(str(race_key), safe='')}"
    )

    r = requests.delete(
        endpoint,
        headers=_headers("return=representation"),
        timeout=20,
    )
    r.raise_for_status()

    try:
        data = r.json()
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


# -----------------------------
# ローカルCSV fallback
# -----------------------------

def _load_local():
    if RESULTS_FILE.exists():
        try:
            df = pd.read_csv(RESULTS_FILE)
        except Exception:
            df = pd.DataFrame(columns=RESULT_COLUMNS)
    else:
        df = pd.DataFrame(columns=RESULT_COLUMNS)

    return _normalize_df(df)


def _write_local(df):
    _normalize_df(df).to_csv(
        RESULTS_FILE,
        index=False,
        encoding="utf-8-sig",
    )


# -----------------------------
# 共通I/O
# -----------------------------

def _load_raw():
    if _use_supabase():
        try:
            return _load_supabase()
        except Exception as e:
            print(
                "[RESULT_TRACKER] Supabase load error:",
                type(e).__name__,
                str(e),
                flush=True,
            )

    return _load_local()


def load_results():
    return _load_raw()


def result_exists(race_key):
    df = _load_raw()

    if len(df) == 0:
        return False

    return (
        df["race_key"]
        .astype(str)
        .eq(str(race_key))
        .any()
    )


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
    Supabase設定済みなら永続保存。
    未設定時のみローカルCSVへ保存。
    """

    final = final.copy()
    tickets = tickets.copy()

    if "p_first" not in final.columns:
        raise ValueError(
            "final に p_first 列がありません。"
        )

    actual_combo = (
        f"{int(first_actual)}-"
        f"{int(second_actual)}-"
        f"{int(third_actual)}"
    )

    ranked_first = (
        final
        .sort_values(
            "p_first",
            ascending=False,
        )
        .reset_index(drop=True)
    )

    p1_lane = _safe_int(
        ranked_first.iloc[0]["lane"]
    )
    p1_prob = _safe_float(
        ranked_first.iloc[0]["p_first"],
        0.0,
    )

    ranked_tickets = tickets.sort_values(
        ["prob", "expected_return"]
        if "expected_return" in tickets.columns
        else ["prob"],
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    if len(ranked_tickets):
        top_ticket = str(
            ranked_tickets.iloc[0].get(
                "combo",
                "",
            )
        )
        top_ticket_prob = _safe_float(
            ranked_tickets.iloc[0].get(
                "prob"
            ),
            0.0,
        )
        top_ticket_odds = _safe_float(
            ranked_tickets.iloc[0].get(
                "odds"
            ),
            np.nan,
        )
        top_ticket_stake = _safe_int(
            ranked_tickets.iloc[0].get(
                "stake"
            ),
            0,
        )
    else:
        top_ticket = ""
        top_ticket_prob = 0.0
        top_ticket_odds = np.nan
        top_ticket_stake = 0

    stake_series = pd.to_numeric(
        tickets.get("stake", 0),
        errors="coerce",
    )

    if not isinstance(
        stake_series,
        pd.Series,
    ):
        total_stake = _safe_int(
            stake_series,
            0,
        )
    else:
        total_stake = int(
            stake_series
            .fillna(0)
            .sum()
        )

    purchased = tickets.copy()

    if "stake" in purchased.columns:
        purchased = purchased[
            pd.to_numeric(
                purchased["stake"],
                errors="coerce",
            )
            .fillna(0)
            > 0
        ]

    hit_any_ticket = (
        actual_combo
        in set(
            purchased.get(
                "combo",
                pd.Series(dtype=str),
            ).astype(str)
        )
    )

    hit_top_ticket = (
        actual_combo
        == top_ticket
    )

    predicted_first_hit = (
        int(first_actual)
        == p1_lane
    )

    payout = _safe_int(
        payout,
        0,
    )

    profit = (
        payout
        - total_stake
    )

    roi = (
        payout / total_stake
        if total_stake > 0
        else np.nan
    )

    keep_cols = [
        "combo",
        "group",
        "prob",
        "odds",
        "expected_return",
        "stake",
    ]

    ticket_payload = []

    for _, row in purchased.iterrows():
        item = {}

        for c in keep_cols:
            if c in row.index:
                item[c] = _json_safe(
                    row[c]
                )

        ticket_payload.append(
            item
        )

    record = {
        "saved_at": datetime.now().isoformat(
            timespec="seconds"
        ),
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
        "hit_top_ticket": bool(
            hit_top_ticket
        ),
        "hit_any_ticket": bool(
            hit_any_ticket
        ),
        "predicted_first_hit": bool(
            predicted_first_hit
        ),
        "tickets_json": json.dumps(
            ticket_payload,
            ensure_ascii=False,
        ),
    }

    if _use_supabase():
        try:
            _upsert_supabase(
                record
            )
            return record
        except Exception as e:
            print(
                "[RESULT_TRACKER] Supabase save error:",
                type(e).__name__,
                str(e),
                flush=True,
            )
            raise RuntimeError(
                "検証データの永続保存に失敗しました。"
            ) from e

    # Supabase未設定時のみ従来CSVへ保存
    df = _load_local()

    df = df[
        df["race_key"]
        .astype(str)
        != str(race_key)
    ]

    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [record]
            ),
        ],
        ignore_index=True,
    )

    _write_local(df)

    return record


def delete_result(race_key):
    if _use_supabase():
        try:
            return _delete_supabase(
                race_key
            )
        except Exception as e:
            print(
                "[RESULT_TRACKER] Supabase delete error:",
                type(e).__name__,
                str(e),
                flush=True,
            )
            return 0

    df = _load_local()
    before = len(df)

    df = df[
        df["race_key"]
        .astype(str)
        != str(race_key)
    ]

    _write_local(df)

    return (
        before
        - len(df)
    )


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

    first_hit = (
        df["predicted_first_hit"]
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
    )

    any_hit = (
        df["hit_any_ticket"]
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
    )

    top_hit = (
        df["hit_top_ticket"]
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
    )

    total_stake = (
        pd.to_numeric(
            df["total_stake"],
            errors="coerce",
        )
        .fillna(0)
        .sum()
    )

    total_payout = (
        pd.to_numeric(
            df["payout"],
            errors="coerce",
        )
        .fillna(0)
        .sum()
    )

    p = pd.to_numeric(
        df["p1_prob"],
        errors="coerce",
    )

    y = first_hit.astype(float)

    valid = p.notna()

    brier = (
        float(
            (
                (
                    p[valid]
                    - y[valid]
                )
                ** 2
            ).mean()
        )
        if valid.any()
        else np.nan
    )

    return {
        "races": int(
            len(df)
        ),
        "first_hit_rate": float(
            first_hit.mean()
        ),
        "ticket_hit_rate": float(
            any_hit.mean()
        ),
        "top_ticket_hit_rate": float(
            top_hit.mean()
        ),
        "total_stake": int(
            total_stake
        ),
        "total_payout": int(
            total_payout
        ),
        "profit": int(
            total_payout
            - total_stake
        ),
        "roi": float(
            total_payout
            / total_stake
        )
        if total_stake > 0
        else np.nan,
        "brier_first": brier,
    }


def calibration_table(df=None):
    if df is None:
        df = _load_raw()

    if len(df) == 0:
        return pd.DataFrame(
            columns=[
                "予測帯",
                "件数",
                "平均予測確率",
                "実的中率",
            ]
        )

    x = df.copy()

    x["p1_prob"] = pd.to_numeric(
        x["p1_prob"],
        errors="coerce",
    )

    x["hit"] = (
        x["predicted_first_hit"]
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
        .astype(float)
    )

    x = x.dropna(
        subset=["p1_prob"]
    )

    if len(x) == 0:
        return pd.DataFrame(
            columns=[
                "予測帯",
                "件数",
                "平均予測確率",
                "実的中率",
            ]
        )

    bins = [
        0,
        .2,
        .4,
        .6,
        .8,
        1.000001,
    ]

    labels = [
        "0-20%",
        "20-40%",
        "40-60%",
        "60-80%",
        "80-100%",
    ]

    x["予測帯"] = pd.cut(
        x["p1_prob"],
        bins=bins,
        labels=labels,
        include_lowest=True,
        right=False,
    )

    g = (
        x.groupby(
            "予測帯",
            observed=False,
        )
        .agg(
            件数=("hit", "size"),
            平均予測確率=("p1_prob", "mean"),
            実的中率=("hit", "mean"),
        )
        .reset_index()
    )

    g["平均予測確率"] = (
        g["平均予測確率"]
        * 100
    ).round(1)

    g["実的中率"] = (
        g["実的中率"]
        * 100
    ).round(1)

    return g
