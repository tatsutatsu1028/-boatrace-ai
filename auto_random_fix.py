from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from official_fetcher import VENUES, fetch_official_race, fetch_odds3t
from prediction import (
    train,
    predict,
    trifecta,
    rank_tickets,
    adaptive_ticket_plan,
    confidence,
    assess_favorite_risk,
    research_prediction_variants,
)
from stake_allocator import allocate_stakes_smart
from today_schedule_fetcher import fetch_today_schedule, fetch_venue_deadlines

JST = ZoneInfo("Asia/Tokyo")
COLLECTOR = "auto_random"
SNAPSHOT_KIND = "auto_random"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# 1日の自動固定数の上限。app_core.py・app.pyの同名の上限と合わせて変更すること。
MAX_DAILY_COUNT = 60
# cronの実行間隔を変えずに1日40〜50件へ対応するため、1回の実行で複数レースを
# まとめて処理する。公式サイトへの1回あたりのアクセス集中を避けるため上限を設ける。
MAX_FIXES_PER_RUN = 3
CONDITIONAL_THIRD_COLUMNS = [
    f"p_third_given_{first_lane}_{second_lane}"
    for first_lane in range(1, 7)
    for second_lane in range(1, 7)
    if first_lane != second_lane
]
LANE_CONTEXT_COLUMNS = [
    "current_meet_avg_finish", "current_meet_avg_finish_adjusted",
    "current_meet_top2_rate",
    "current_meet_avg_st", "current_meet_races",
    "course_top3_rate", "course_avg_st", "course_start_rank",
    "venue_course_1st", "venue_course_2nd", "venue_course_3rd",
    "venue_course_4th", "venue_course_5th", "venue_course_6th",
]
SNAPSHOT_FEATURE_COLUMNS = [
    "lane", "racer_id", "racer_name", "racer_class", "avg_st",
    "racer_win_rate", "local_win_rate", "motor_2ren", "boat_2ren",
    "weight", "tilt", "exhibition_time", "exhibition_st",
    "original_straight", "original_turn", "original_lap",
    "current_meet_avg_finish", "current_meet_avg_finish_adjusted",
    "current_meet_top2_rate",
    "current_meet_avg_st", "current_meet_races",
    "course_top3_rate", "course_avg_st", "course_start_rank",
    "venue_course_1st", "venue_course_2nd", "venue_course_3rd",
    "venue_course_4th", "venue_course_5th", "venue_course_6th",
    "venue_kimarite_nige", "venue_kimarite_makuri",
    "venue_kimarite_sashi", "venue_kimarite_makuri_sashi",
    "venue_kimarite_nuki", "venue_kimarite_megumare",
    "wind_speed", "wave_height", "temperature",
]


def _cfg():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です。")
    return url, key


def _headers(prefer=None):
    _, key = _cfg()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _request(method, url, *, attempts=3, accepted_status=(), **kwargs):
    """一時的な接続失敗とData APIの5xxを指数バックオフで再試行する。"""
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in accepted_status:
                return response
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"{response.status_code} Server Error for url: {response.url}",
                response=response,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc

        if attempt + 1 < attempts:
            delay = 2 ** attempt
            print(
                f"[AUTO_RANDOM] transient API error; retry "
                f"{attempt + 2}/{attempts} in {delay}s: {last_error}"
            )
            time.sleep(delay)

    raise last_error


def _load_settings():
    url, _ = _cfg()
    r = _request(
        "GET",
        f"{url}/rest/v1/app_settings?id=eq.1&select=*",
        headers=_headers(),
        timeout=15,
    )
    rows = r.json() or []
    if not rows:
        raise RuntimeError("app_settings id=1 がありません。")
    return rows[0]


def _runtime_settings(settings):
    style = str(settings.get("prediction_style", "バランス"))
    default_display = 0.42 if style == "展示重視" else 0.32
    return {
        "main_n": int(settings.get("main_n", 3) or 3),
        "cover_n": int(settings.get("cover_n", 3) or 3),
        "hole_n": int(settings.get("hole_n", 0) or 0),
        "total_budget": int(settings.get("total_budget", 2000) or 2000),
        "min_bet": int(settings.get("min_bet", 100) or 100),
        "longshot_min_prob_pct": float(settings.get("longshot_min_prob_pct", 0.30) or 0.30),
        "value_bias": float(settings.get("value_bias", 0.0) or 0.0),
        "prediction_style": style,
        "display_weight": float(settings.get("display_weight", default_display)),
        "weather_weight": float(settings.get("weather_weight", 0.10)),
        "venue_course_weight": float(settings.get("venue_course_weight", 0.12)),
        "hedge_enabled": bool(settings.get("hedge_enabled", True)),
    }


def _run_auto_count(date_text, started_at):
    url, _ = _cfg()
    params = {
        "select": "race_key",
        "race_date": f"eq.{date_text}",
        "collector_name": f"eq.{COLLECTOR}",
    }
    if started_at:
        params["saved_at"] = f"gte.{started_at}"
    r = _request(
        "GET",
        f"{url}/rest/v1/prediction_snapshots",
        params=params,
        headers=_headers(),
        timeout=15,
    )
    return len(r.json() or [])


def _snapshot_exists(race_key):
    url, _ = _cfg()
    r = _request(
        "GET",
        f"{url}/rest/v1/prediction_snapshots",
        params={"select": "race_key", "race_key": f"eq.{race_key}", "limit": "1"},
        headers=_headers(),
        timeout=15,
    )
    return bool(r.json() or [])


def _json_safe(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        v = float(v)
        return v if np.isfinite(v) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _snapshot_payload(
    final,
    tickets,
    research_variants=None,
    confidence_label=None,
    ticket_plan=None,
    race_features=None,
):
    final_cols = [
        "lane", "racer_name", "p_first", "p_second", "p_third",
        "p_second_given_1", "p_second_given_2", "p_second_given_3",
        "p_second_given_4", "p_second_given_5", "p_second_given_6",
        *CONDITIONAL_THIRD_COLUMNS,
        "model_version", "reason", "kimarite_adjustment", "kimarite_effect_pct",
        "kimarite_starts", "kimarite_wins", "kimarite_dominant", "kimarite_available",
        *LANE_CONTEXT_COLUMNS,
    ]
    ticket_cols = [
        "combo", "group", "prob", "odds", "expected_return", "stake", "stake_reason",
        "recommended",
    ]

    final_rows = []
    for _, row in final.sort_values("lane").iterrows():
        final_rows.append({c: _json_safe(row[c]) for c in final_cols if c in row.index})

    ticket_rows = []
    for _, row in tickets.iterrows():
        ticket_rows.append({c: _json_safe(row[c]) for c in ticket_cols if c in row.index})

    research_payload = {}
    if research_variants:
        for label, variant_df in research_variants.items():
            if variant_df is None or len(variant_df) == 0:
                continue
            rows = []
            for _, row in variant_df.sort_values("lane").iterrows():
                item = {}
                for c in (
                    "lane", "racer_name", "p_first", "p_second", "p_third",
                    "model_version", "reason", "challenger_score",
                    "challenger_evidence", "challenger_delta",
                    "challenger_version",
                ):
                    if c in row.index:
                        item[c] = _json_safe(row[c])
                rows.append(item)
            research_payload[str(label)] = rows

    payload = {
        "feature_schema_version": 1,
        "final": final_rows,
        "tickets": ticket_rows,
        "research": research_payload,
        "race_features": [],
    }
    if race_features is not None and len(race_features):
        for _, row in race_features.sort_values("lane").iterrows():
            payload["race_features"].append(
                {
                    column: _json_safe(row[column])
                    for column in SNAPSHOT_FEATURE_COLUMNS
                    if column in row.index
                }
            )
    label = str(confidence_label or "").strip()
    if label in {"A", "B", "C"}:
        payload["confidence"] = label
    if ticket_plan:
        payload["ticket_plan"] = {
            str(key): _json_safe(value) for key, value in ticket_plan.items()
        }
    return payload


def _save_snapshot(
    race_key,
    date_text,
    venue,
    rno,
    final,
    tickets,
    research_variants=None,
    confidence_label=None,
    ticket_plan=None,
    race_features=None,
):
    if _snapshot_exists(race_key):
        print("[AUTO_RANDOM] already fixed:", race_key)
        return False

    url, _ = _cfg()
    record = {
        "race_key": race_key,
        "collector_name": COLLECTOR,
        "saved_at": datetime.now(JST).isoformat(timespec="seconds"),
        "race_date": date_text,
        "venue": venue,
        "race_no": int(rno),
        "snapshot_kind": SNAPSHOT_KIND,
        "payload_json": json.dumps(
            _snapshot_payload(
                final,
                tickets,
                research_variants=research_variants,
                confidence_label=confidence_label,
                ticket_plan=ticket_plan,
                race_features=race_features,
            ),
            ensure_ascii=False,
        ),
    }
    r = _request(
        "POST",
        f"{url}/rest/v1/prediction_snapshots",
        headers=_headers("return=minimal"),
        json=record,
        timeout=20,
        accepted_status={409},
    )
    if r.status_code == 409:
        # 直前のPOSTがタイムアウト後にDB側で完了した場合も含む。
        print("[AUTO_RANDOM] race already fixed after save attempt:", race_key)
    return True


def _add_to_odds_watchlist(race_date, jcd, rno):
    """手動固定と同じく、固定後にオッズ追跡へ登録する。"""
    url, _ = _cfg()
    payload = {
        "race_date": str(race_date),
        "jcd": str(jcd).zfill(2),
        "rno": int(rno),
        "active": True,
    }
    _request(
        "POST",
        f"{url}/rest/v1/odds_watchlist?on_conflict=race_date,jcd,rno",
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=payload,
        timeout=15,
    )


def _save_odds_snapshot_now(race_date, jcd, rno, odds_df):
    """固定時点のオッズを1回保存し、B/C/Dの追跡起点を手動固定と揃える。"""
    if odds_df is None or len(odds_df) == 0:
        return 0

    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    payload = []
    for _, row in odds_df.iterrows():
        combo = str(row.get("combo", "")).strip()
        try:
            odd = float(row.get("odds"))
        except Exception:
            continue
        if not combo or odd < 1:
            continue
        payload.append({
            "race_date": str(race_date),
            "jcd": str(jcd).zfill(2),
            "rno": int(rno),
            "combo": combo,
            "odds": odd,
            "fetched_at": fetched_at,
        })

    if not payload:
        return 0

    url, _ = _cfg()
    _request(
        "POST",
        f"{url}/rest/v1/odds_snapshots",
        headers=_headers("return=minimal"),
        json=payload,
        timeout=30,
    )
    return len(payload)


def _deadline_is_safe(today, hhmm, margin_minutes=15, max_margin_minutes=None):
    try:
        hour, minute = [int(x) for x in str(hhmm).split(":")]
        deadline = datetime(today.year, today.month, today.day, hour, minute, tzinfo=JST)
        remaining = (deadline - datetime.now(JST)).total_seconds()
        if remaining < margin_minutes * 60:
            return False
        if max_margin_minutes is not None and remaining > max_margin_minutes * 60:
            return False
        return True
    except Exception:
        return False


def _exhibition_ready(race):
    """6艇すべての公式展示タイムが取得できたレースだけを固定対象にする。"""
    if race is None or len(race) != 6 or "exhibition_time" not in race.columns:
        return False

    lanes = pd.to_numeric(race.get("lane"), errors="coerce")
    times = pd.to_numeric(race["exhibition_time"], errors="coerce")
    valid = lanes.between(1, 6) & times.between(6.0, 8.5)
    return bool(valid.sum() == 6 and lanes[valid].nunique() == 6)


def _list_candidates(today):
    """
    締切15〜45分前かつ未固定の候補一覧を、開催中の全会場から集める。

    公式展示タイムは締切のかなり手前（1時間以上前）では公開されないため、
    展示公開が見込めない締切too-far先のレースまで候補に含めると、
    fetch_official_race のリクエストを無駄打ちすることになる
    （実運用で1候補あたり約20秒・全て0/6という無駄打ちが多数発生する
    ことを確認済み）。締切45分以内に絞ることでこの無駄打ちを減らす。

    会場・レースの並びはランダムにシャッフルする。
    """
    date_key = today.strftime("%Y%m%d")
    schedule = fetch_today_schedule(date_key)
    holding = schedule[schedule["holding"].astype(bool)].copy()
    codes = holding["jcd"].astype(str).str.zfill(2).tolist()
    random.shuffle(codes)

    candidates = []
    for jcd in codes:
        try:
            deadlines = fetch_venue_deadlines(date_key, jcd)
        except Exception as e:
            print("[AUTO_RANDOM] deadline error", jcd, type(e).__name__, e)
            continue

        for rno, hhmm in deadlines.items():
            if not _deadline_is_safe(today, hhmm, margin_minutes=15, max_margin_minutes=45):
                continue
            race_key = f"{date_key}_{jcd}_{int(rno)}"
            if _snapshot_exists(race_key):
                continue
            candidates.append((jcd, int(rno), race_key))

    random.shuffle(candidates)
    return candidates


def _pick_ready_candidates(today, candidates, max_needed):
    """
    候補を順に確認し、公式展示タイムが6艇分そろったレースを
    最大max_needed件返す。展示未公開の候補は固定せずスキップする。

    確認時に取得したrace DataFrameをそのまま予想に使い、
    同じレースの再取得を避ける。
    """
    date_key = today.strftime("%Y%m%d")
    ready = []

    for jcd, rno, race_key in candidates:
        if len(ready) >= max_needed:
            break

        try:
            race = fetch_official_race(date_key, jcd, rno)
        except Exception as e:
            print(
                "[AUTO_RANDOM] official data error",
                race_key,
                type(e).__name__,
                e,
            )
            continue

        if not _exhibition_ready(race):
            count = 0
            if race is not None and "exhibition_time" in race.columns:
                count = int(pd.to_numeric(race["exhibition_time"], errors="coerce").notna().sum())
            print(
                f"[AUTO_RANDOM] exhibition not ready: {race_key} ({count}/6); skip"
            )
            continue

        print(f"[AUTO_RANDOM] exhibition ready: {race_key} (6/6)")
        ready.append((jcd, rno, race_key, race))

    return ready


def _process_candidate(model, runtime, today, jcd, rno, race_key, race):
    """
    展示情報つきで確認済みの1レースを予想・買い目生成し、スナップショットとして
    保存する。データ不備など回収可能な問題はFalseを返してスキップし、
    他の候補の処理を止めない。
    """
    date_key = today.strftime("%Y%m%d")
    date_text = today.isoformat()
    venue = VENUES.get(jcd, jcd)
    print("[AUTO_RANDOM] selected", race_key, venue, f"{rno}R", "exhibition=6/6")

    odds = fetch_odds3t(date_key, jcd, rno)
    if odds is None or len(odds) < 100:
        print(f"[AUTO_RANDOM] insufficient odds data: {race_key}; skip")
        return False

    final = predict(
        model,
        race,
        display_weight=runtime["display_weight"],
        weather_weight=runtime["weather_weight"],
        venue_course_weight=runtime["venue_course_weight"],
        original_display_scale=0.0,
    )
    confidence_label = confidence(final, race)

    research_variants = research_prediction_variants(
        model,
        race,
        display_weight=runtime["display_weight"],
        weather_weight=runtime["weather_weight"],
        venue_course_weight=runtime["venue_course_weight"],
    )

    tri = trifecta(final)
    ticket_plan = adaptive_ticket_plan(final)
    target_points = int(ticket_plan["point_count"])
    main_points = min(int(ticket_plan["main_n"]), target_points)
    cover_points = target_points - main_points

    favorite_lane, risk_score, _risk_reasons = assess_favorite_risk(race, final)
    hedge_lane = (
        favorite_lane
        if runtime["hedge_enabled"] and risk_score >= 2
        else None
    )

    tickets = rank_tickets(
        tri,
        odds=odds,
        main_n=main_points,
        cover_n=cover_points,
        longshot_n=0,
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0,
        hedge_lane=hedge_lane,
        use_odds=False,
        first=final,
        min_first_margin=0.40,
        min_second_coverage=ticket_plan["min_second_coverage"],
        close_third_gap=None,
        close_third_coverage=4,
        include_nonrecommended=True,
        second_favorite_n=2,
    )
    if len(tickets) != target_points:
        raise RuntimeError(
            f"買い目点数の生成不整合: 予定{target_points}点 / 実際{len(tickets)}点"
        )
    tickets = allocate_stakes_smart(
        tickets,
        budget=runtime["total_budget"],
        unit=100,
        min_bet=runtime["min_bet"],
        max_longshot_share=0.15,
        max_ticket_share=0.35,
        value_bias=runtime["value_bias"],
        use_odds=False,
        guarantee_col="second_favorite",
    )
    if (
        "recommended" in tickets.columns
        and not tickets["recommended"].fillna(True).all()
    ):
        tickets["stake"] = 0
        tickets["stake_reason"] = "非推奨のためシミュレーション投資なし"

    if not _save_snapshot(
        race_key,
        date_text,
        venue,
        rno,
        final,
        tickets,
        research_variants=research_variants,
        confidence_label=confidence_label,
        ticket_plan=ticket_plan,
        race_features=race,
    ):
        return False

    # 手動固定と同じく、固定直後からオッズ追跡を開始し、初回値も保存する。
    try:
        _add_to_odds_watchlist(date_key, jcd, rno)
        saved_odds = _save_odds_snapshot_now(date_key, jcd, rno, odds)
        print(f"[AUTO_RANDOM] odds tracking started: {race_key}; first={saved_odds}")
    except Exception as e:
        # 固定自体は成功しているため、追跡失敗で固定を取り消さない。
        print(
            "[AUTO_RANDOM] odds tracking start error",
            race_key,
            type(e).__name__,
            str(e),
        )

    print(f"[AUTO_RANDOM] saved {race_key}; confidence={confidence_label}")
    return True


def main():
    settings = _load_settings()
    runtime = _runtime_settings(settings)
    enabled = bool(settings.get("random_auto_enabled", False))
    target = int(settings.get("random_auto_daily_count", 3) or 3)
    target = max(1, min(target, MAX_DAILY_COUNT))
    started_at = settings.get("random_auto_started_at")

    if not enabled:
        print("[AUTO_RANDOM] OFF")
        return

    now = datetime.now(JST)
    today = now.date()
    date_text = today.isoformat()
    current = _run_auto_count(date_text, started_at)
    if current >= target:
        # ONのまま据え置く。race_dateで日付ごとに数えているため、
        # 日付が変わればcurrentは自動的に0に戻り、翌日また自動固定を再開する。
        # ここでOFFに戻すと、翌日以降のcron実行がずっと[AUTO_RANDOM] OFFのまま
        # 何もせず終わり続け、手動でONに戻すまでデータが止まってしまう。
        print(f"[AUTO_RANDOM] target reached for today: {current}/{target}; wait for next day")
        return

    # cronの実行間隔を増やさずに1日の目標件数へ近づけるため、1回の実行で
    # 残り件数とMAX_FIXES_PER_RUNの小さい方まで複数レースをまとめて処理する。
    run_cap = min(target - current, MAX_FIXES_PER_RUN)
    candidates = _list_candidates(today)
    ready = _pick_ready_candidates(today, candidates, run_cap)
    if not ready:
        print("[AUTO_RANDOM] no unfixed race with complete exhibition data found")
        return

    # 本番学習データは手動固定と共通の sample_history.csv に固定。学習は
    # このプロセス内で1回だけ行い、今回処理する候補すべてで使い回す。
    history = pd.read_csv(Path(__file__).with_name("sample_history.csv"))
    model = train(history)

    saved = 0
    for jcd, rno, race_key, race in ready:
        try:
            if _process_candidate(model, runtime, today, jcd, rno, race_key, race):
                saved += 1
        except Exception as e:
            print(
                "[AUTO_RANDOM] process error",
                race_key,
                type(e).__name__,
                str(e),
            )

    new_count = current + saved
    print(
        f"[AUTO_RANDOM] run finished; saved {saved}/{len(ready)} this run; "
        f"{new_count}/{target}"
    )
    if new_count >= target:
        print(f"[AUTO_RANDOM] target reached for today: {new_count}/{target}; wait for next day")


if __name__ == "__main__":
    main()
