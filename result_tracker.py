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
SETTINGS_FILE = Path(__file__).parent / "app_settings.json"

SUPABASE_TABLE = "prediction_results"
SUPABASE_SETTINGS_TABLE = "app_settings"
SUPABASE_SNAPSHOT_TABLE = "prediction_snapshots"


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
    "lane_probs_json",
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
# 予想時点スナップショット
# -----------------------------

def _snapshot_payload(final, tickets, research_variants=None):
    final_rows = []
    for _, row in final.sort_values("lane").iterrows():
        item = {}
        for c in ("lane", "racer_name", "p_first", "reason"):
            if c in row.index:
                item[c] = _json_safe(row[c])
        final_rows.append(item)

    ticket_rows = []
    for _, row in tickets.iterrows():
        item = {}
        for c in (
            "combo", "group", "prob", "odds",
            "expected_return", "stake", "stake_reason",
        ):
            if c in row.index:
                item[c] = _json_safe(row[c])
        ticket_rows.append(item)

    research_payload = {}
    if research_variants:
        for label, variant_df in research_variants.items():
            if variant_df is None or len(variant_df) == 0:
                continue
            rows = []
            for _, row in variant_df.sort_values("lane").iterrows():
                item = {}
                for c in ("lane", "racer_name", "p_first", "reason"):
                    if c in row.index:
                        item[c] = _json_safe(row[c])
                rows.append(item)
            research_payload[str(label)] = rows

    return {
        "final": final_rows,
        "tickets": ticket_rows,
        "research": research_payload,
    }


def load_prediction_snapshot(race_key):
    """
    race_key に対してレース前に固定した予想を1件取得する。
    テーブル未作成・未設定時は None。
    """
    if not _use_supabase():
        return None

    url, _ = _supabase_config()
    endpoint = (
        f"{url}/rest/v1/{SUPABASE_SNAPSHOT_TABLE}"
        f"?race_key=eq.{requests.utils.quote(str(race_key), safe='')}"
        "&select=*"
        "&limit=1"
    )

    try:
        r = requests.get(
            endpoint,
            headers=_headers(),
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(
            "[RESULT_TRACKER] snapshot load error:",
            type(e).__name__,
            str(e),
            flush=True,
        )
        return None

    if not data:
        return None

    return data[0]


def prediction_snapshot_exists(race_key):
    return load_prediction_snapshot(race_key) is not None


def save_prediction_snapshot(
    race_key,
    race_date,
    venue,
    race_no,
    final,
    tickets,
    research_variants=None,
    snapshot_kind="same_day",
):
    """
    AI予想を「その時点のまま」固定する。

    同じ race_key が既に固定済みなら上書きしない。
    これによりレース終了後に再取得・再計算しても、
    検証保存では最初に固定した予想を使える。
    """
    existing = load_prediction_snapshot(race_key)
    if existing is not None:
        return existing

    if not _use_supabase():
        raise RuntimeError(
            "予想スナップショットはSupabase接続時のみ利用できます。"
        )

    payload = _snapshot_payload(
        final.copy(),
        tickets.copy(),
        research_variants=research_variants,
    )

    record = {
        "race_key": str(race_key),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "race_date": str(race_date),
        "venue": str(venue),
        "race_no": int(race_no),
        "snapshot_kind": str(snapshot_kind),
        "payload_json": json.dumps(
            payload,
            ensure_ascii=False,
        ),
    }

    url, _ = _supabase_config()
    endpoint = f"{url}/rest/v1/{SUPABASE_SNAPSHOT_TABLE}"

    r = requests.post(
        endpoint,
        headers=_headers("return=representation"),
        json=record,
        timeout=20,
    )

    # 同時操作等で先に作成された場合は、その既存値を採用。
    if r.status_code == 409:
        existing = load_prediction_snapshot(race_key)
        if existing is not None:
            return existing

    r.raise_for_status()

    try:
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception:
        pass

    return record


def _restore_snapshot_frames(snapshot):
    if not snapshot:
        return None, None, None

    raw = snapshot.get("payload_json", "")
    if not raw:
        return None, None, None

    try:
        payload = json.loads(raw)
    except Exception:
        return None, None, None

    if not isinstance(payload, dict):
        return None, None, None

    final_rows = payload.get("final", [])
    ticket_rows = payload.get("tickets", [])
    research_raw = payload.get("research", {})

    final_df = pd.DataFrame(final_rows) if isinstance(final_rows, list) else None
    tickets_df = pd.DataFrame(ticket_rows) if isinstance(ticket_rows, list) else None

    research = {}
    if isinstance(research_raw, dict):
        for label, rows in research_raw.items():
            if isinstance(rows, list) and rows:
                research[str(label)] = pd.DataFrame(rows)

    return final_df, tickets_df, research


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
    research_variants=None,
    prefer_snapshot=True,
):
    """
    AI予想と実着順を1レース分保存。
    Supabase設定済みなら永続保存。
    未設定時のみローカルCSVへ保存。
    """

    snapshot_used = None
    if prefer_snapshot:
        snapshot_used = load_prediction_snapshot(race_key)
        snap_final, snap_tickets, snap_research = _restore_snapshot_frames(
            snapshot_used
        )
        if (
            snap_final is not None
            and len(snap_final)
            and snap_tickets is not None
        ):
            final = snap_final
            tickets = snap_tickets
            research_variants = snap_research

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

    # 6艇全員の予測確率を保存しておくと、後から「AI予想全体 vs 実際の結果」
    # を1レース単位で振り返れる。final には lane, racer_name, p_first, reason
    # が含まれている想定。
    lane_keep_cols = ["lane", "racer_name", "p_first", "reason"]
    lane_payload = []

    for _, row in final.sort_values("lane").iterrows():
        item = {}

        for c in lane_keep_cols:
            if c in row.index:
                item[c] = _json_safe(row[c])

        lane_payload.append(item)

    # 研究用の段階別予想も、既存の lane_probs_json 列に同居させる。
    # DB列を増やさないためSupabase側のマイグレーションは不要。
    # 旧データ（list形式）もapp.py側で引き続き読めるようにしている。
    research_payload = {}
    if research_variants:
        for label, variant_df in research_variants.items():
            if variant_df is None or len(variant_df) == 0:
                continue

            rows = []
            for _, vrow in variant_df.sort_values("lane").iterrows():
                item = {}
                for c in ("lane", "racer_name", "p_first", "reason"):
                    if c in vrow.index:
                        item[c] = _json_safe(vrow[c])
                rows.append(item)

            research_payload[str(label)] = rows

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
        "lane_probs_json": json.dumps(
            {
                "final": lane_payload,
                "research": research_payload,
                "snapshot": {
                    "saved_at": snapshot_used.get("saved_at", ""),
                    "kind": snapshot_used.get("snapshot_kind", ""),
                } if snapshot_used else None,
            }
            if (research_payload or snapshot_used)
            else lane_payload,
            ensure_ascii=False,
        ),
    }

    if _use_supabase():
        try:
            _upsert_supabase(
                record
            )
            record["_used_snapshot"] = bool(snapshot_used)
            record["_snapshot_saved_at"] = snapshot_used.get("saved_at", "") if snapshot_used else ""
            record["_snapshot_kind"] = snapshot_used.get("snapshot_kind", "") if snapshot_used else ""
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

    record["_used_snapshot"] = bool(snapshot_used)
    record["_snapshot_saved_at"] = snapshot_used.get("saved_at", "") if snapshot_used else ""
    record["_snapshot_kind"] = snapshot_used.get("snapshot_kind", "") if snapshot_used else ""
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


# -----------------------------
# アプリ設定の保存・復元
# -----------------------------
# コンテナ再起動でst.session_stateが失われても、買い目設定
# （本線/抑え/穴の点数、予算、妙味重視度など）を復元できるようにする。
# 保存先の優先順位はレース結果と同じ：Supabase設定済みならSupabase、
# 未設定ならローカルJSON（Streamlit Cloudでは再起動時に消えるため
# あくまでローカル実行時のフォールバック）。

DEFAULT_SETTINGS = {
    "main_n": 3,
    "cover_n": 3,
    "hole_n": 2,
    "total_budget": 2000,
    "min_bet": 100,
    "longshot_min_prob_pct": 0.30,
    "value_bias": 0.0,
    "prediction_style": "バランス",
}


def load_settings():
    """保存済みの設定を返す。無ければDEFAULT_SETTINGSを返す。"""
    if _use_supabase():
        try:
            url, _ = _supabase_config()
            endpoint = f"{url}/rest/v1/{SUPABASE_SETTINGS_TABLE}?select=*&id=eq.1"
            r = requests.get(endpoint, headers=_headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            if data:
                merged = dict(DEFAULT_SETTINGS)
                merged.update({k: v for k, v in data[0].items() if k in DEFAULT_SETTINGS})
                return merged
        except Exception as e:
            print("[RESULT_TRACKER] Supabase settings load error:", type(e).__name__, str(e), flush=True)
    else:
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
                return merged
        except Exception as e:
            print("[RESULT_TRACKER] local settings load error:", type(e).__name__, str(e), flush=True)

    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict):
    """設定を保存する。既存の1件を上書き（id=1固定）する。"""
    payload = {k: _json_safe(settings.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}

    if _use_supabase():
        try:
            url, _ = _supabase_config()
            endpoint = f"{url}/rest/v1/{SUPABASE_SETTINGS_TABLE}?on_conflict=id"
            payload["id"] = 1
            r = requests.post(
                endpoint,
                headers=_headers("resolution=merge-duplicates,return=minimal"),
                json=payload,
                timeout=15,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            print("[RESULT_TRACKER] Supabase settings save error:", type(e).__name__, str(e), flush=True)
            return False

    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return True
    except Exception as e:
        print("[RESULT_TRACKER] local settings save error:", type(e).__name__, str(e), flush=True)
        return False


# -----------------------------
# オッズ時系列追跡
# -----------------------------
# GitHub Actions（track_odds.py）が5分おきにオッズを記録する仕組み。
# ここではその「追跡対象への登録」と「記録された履歴の取得」のみ扱う。
# Supabase未設定の場合はこの機能自体を使えない（ローカルCSVでの
# 時系列追跡は現実的でないため）。

SUPABASE_WATCHLIST_TABLE = "odds_watchlist"
SUPABASE_ODDS_TABLE = "odds_snapshots"


def odds_tracking_available():
    """Supabaseが設定されているかどうか。UI側の表示切り替えに使う。"""
    return _use_supabase()


def add_to_odds_watchlist(race_date, jcd, rno):
    """このレースをオッズ追跡対象に登録する。既存なら再アクティブ化する。"""
    if not _use_supabase():
        return False, "Supabaseが設定されていないため、オッズ追跡は使えません。"

    try:
        url, _ = _supabase_config()
        endpoint = f"{url}/rest/v1/{SUPABASE_WATCHLIST_TABLE}?on_conflict=race_date,jcd,rno"
        payload = {
            "race_date": str(race_date),
            "jcd": str(jcd).zfill(2),
            "rno": int(rno),
            "active": True,
        }
        r = requests.post(
            endpoint,
            headers=_headers("resolution=merge-duplicates,return=minimal"),
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return True, "このレースの追跡を開始しました（数分おきに自動でオッズを記録します）。"
    except Exception as e:
        print("[RESULT_TRACKER] watchlist add error:", type(e).__name__, str(e), flush=True)
        return False, f"追跡登録に失敗しました: {e}"


def load_odds_history(race_date, jcd, rno):
    """指定レースのオッズ推移（combo, odds, fetched_at）をDataFrameで返す。"""
    if not _use_supabase():
        return pd.DataFrame(columns=["combo", "odds", "fetched_at"])

    try:
        url, _ = _supabase_config()
        endpoint = (
            f"{url}/rest/v1/{SUPABASE_ODDS_TABLE}"
            f"?select=combo,odds,fetched_at"
            f"&race_date=eq.{str(race_date)}"
            f"&jcd=eq.{str(jcd).zfill(2)}"
            f"&rno=eq.{int(rno)}"
            f"&order=fetched_at.asc"
        )
        r = requests.get(endpoint, headers=_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()

        if not data:
            return pd.DataFrame(columns=["combo", "odds", "fetched_at"])

        df = pd.DataFrame(data)
        df["fetched_at"] = pd.to_datetime(df["fetched_at"])
        return df
    except Exception as e:
        print("[RESULT_TRACKER] odds history load error:", type(e).__name__, str(e), flush=True)
        return pd.DataFrame(columns=["combo", "odds", "fetched_at"])


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
