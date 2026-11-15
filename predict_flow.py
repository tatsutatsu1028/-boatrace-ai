"""
「予想」ボタンの中身（予想 → Claude → 保存）。アプリの中で計算するときと、GitHub Actions で計算するとき
（remote_predict.py）で同じものを使う。もとは app_core.py の予想ボタンの処理と _auto_save_prediction。

予想ロジック本体は model_only.py / prediction.py のまま。ここは呼ぶ順番と、保存のルールだけ:
  - 締切前は最新の予想で上書きし、締切後は上書きしない（過去日で未保存のときだけバックテストとして保存）
  - 自動固定で保存済みのレースは、締切前でも上書きしない
  - 同じ内容（payload_hash と展示前の印が同じ）なら書き込まない
  - 当日レースを初めて保存したら、オッズ追跡にも登録する
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

import perf

JST = ZoneInfo("Asia/Tokyo")


def today_jst():
    return datetime.now(JST).date()


def exhibition_ready(race):
    """全艇の展示タイムが揃っているか（自動固定の判定と同じ基準）。"""
    if race is None or len(race) == 0 or "exhibition_time" not in race.columns:
        return False
    times = pd.to_numeric(race["exhibition_time"], errors="coerce")
    return bool(times.notna().all())


def deadline_minutes_left(d, hhmm, now=None):
    """日本時間で締切までの残り分数。読めなければ None。"""
    try:
        hour, minute = map(int, str(hhmm).split(":"))
        deadline = datetime(d.year, d.month, d.day, hour, minute, tzinfo=JST)
        return (deadline - (now or datetime.now(JST))).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None


def deadline_passed(d, deadlines, rno, now=None):
    """当日レースの締切を過ぎていれば True。過去日も True。"""
    today = (now or datetime.now(JST)).date()
    if d < today:
        return True
    if d > today:
        return False
    hhmm = (deadlines or {}).get(rno)
    mins = deadline_minutes_left(d, hhmm, now) if hhmm else None
    return mins is not None and mins <= 0


def compute(model, work, odds, params, model_setting, date8, jcd, rno, ctx):
    """予想と買い目（「モデルのみ予想」と同じ）。戻り値は予想タブが st.session_state["result"] に置く dict。

    model が None（保存済みの今までのモデルが読めない）なら、新しいモデルで予想できるレースだけ予想する
    （できなければ legacy_model.LegacyModelUnavailable）。
    「買い目」の時間には、入れ子の区間（新しいモデル・今までのモデルの予想）を引いた残りが入る。
    """
    import legacy_model
    from model_only import model_only_prediction

    with perf.section("買い目"):
        if model is None:
            mo = legacy_model.new_model_only_prediction(work, odds, params, model_setting, date8, jcd, rno)
        else:
            mo = model_only_prediction(model, work, odds, params, model_setting, date8, jcd, rno)
    return {
        "context": ctx,
        "before": mo["before"],
        "before_label": mo["before_label"],
        "model_info": mo["model_info"],
        "final": mo["final"],
        "tickets": mo["tickets"],
        "work": work,
        "hedge_lane": mo["hedge_lane"],
        "risk_reasons": mo["risk_reasons"],
        "research_variants": mo["research_variants"],
        "ticket_plan": mo["ticket_plan"],
        # 展示データが揃う前の予想かどうか（保存する予想にも印を付ける）。
        "pre_exhibition": not exhibition_ready(work),
    }


def claude_step(result, ctx, label, work, odds, claude_params, mode, api_key, read_model=None):
    """管理者だけ: 3つの予想（Claudeのみ・両方の平均を押したときだけ Claude を1回呼ぶ。モデルのみのときは
    呼ばず、前に取った予想があれば一緒に保存する）。失敗してもモデルの予想と買い目はそのまま出す。
    result に "mode" と "claude" を入れる。"""
    import json

    import claude_tab
    from model_only import complete_adaptive_tickets
    from result_tracker import load_prediction_snapshot

    result["mode"] = mode
    saved_section = None
    try:
        snap = load_prediction_snapshot(ctx)
        saved_section = (json.loads((snap or {}).get("payload_json") or "{}") or {}).get("claude")
    except Exception:  # noqa: BLE001
        saved_section = None
    with perf.section("Claude"):
        result["claude"] = claude_tab.run_at_predict(
            ctx, label, work, result["final"], result["tickets"], odds, claude_params, complete_adaptive_tickets,
            api_key=api_key, model=read_model or None, saved_section=saved_section, call_api=mode != "model",
        )
    claude_tab.set_mode(ctx, mode)
    return result


def build_payload(result):
    """保存する payload_json とその hash（result["payload_hash"] にも入れる）。"""
    import prediction
    from result_tracker import build_snapshot_payload_json

    # AI総合信頼度(A/B/C)は confidence() を呼んだ時に final へ記録されるので、画面表示より先に保存する今は、
    # ここで一度計算して payload に含める。
    try:
        prediction.confidence(result["final"], result.get("work"))
    except Exception:  # noqa: BLE001
        pass
    payload_json, payload_hash = build_snapshot_payload_json(
        result["final"],
        result["tickets"],
        research_variants=result.get("research_variants") or {},
        race_features=result.get("work"),
        pre_exhibition=result.get("pre_exhibition", False),
        extra={
            **({"claude": result["claude"]} if result.get("claude") else {}),
            # どちらの予想モデルで出したか（設定値・使った版・新しいモデルを使えなかった理由）
            **({"prediction_model": result["model_info"]} if result.get("model_info") else {}),
        } or None,
    )
    result["payload_hash"] = payload_hash
    return payload_json, payload_hash


def save(ctx, d, jcd, venue, rno, deadlines, odds, result, collector_name, meta, now=None):
    """「AI最終予想」を押した時点の予想を prediction_snapshots へ保存する（ルールはこのファイルの先頭）。

    meta: 保存済み予想の状態（{"exists": bool, ...}。読めなければ None）。
    戻り値は表示用の dict（status / message / meta / odds_message / odds_warning）。
    """
    from result_tracker import (
        add_to_odds_watchlist,
        odds_tracking_available,
        save_latest_prediction_snapshot,
        save_odds_snapshot_now,
    )

    with perf.section("保存"):
        payload_json, payload_hash = build_payload(result)
        if meta is None:
            return {"status": "error", "message": "保存済み予想の確認に失敗しました。"}
        if meta["exists"] and meta.get("snapshot_kind") == "auto_random":
            # 自動固定で保存済みのレースは、締切前でも上書きしない。
            return {"status": "auto_fixed", "meta": meta}
        today = (now or datetime.now(JST)).date()
        is_past = d < today
        if deadline_passed(d, deadlines, rno, now) and (meta["exists"] or not is_past):
            # 締切後は上書きしない。過去日で未保存のときだけバックテストとして保存する。
            return {"status": "closed", "meta": meta}
        if (
            meta["exists"]
            and meta.get("payload_hash") == payload_hash
            and bool(meta.get("pre_exhibition")) == bool(result.get("pre_exhibition"))
        ):
            return {"status": "unchanged", "meta": meta}
        try:
            saved = save_latest_prediction_snapshot(
                race_key=ctx,
                venue=venue,
                payload_json=payload_json,
                payload_hash=payload_hash,
                pre_exhibition=result.get("pre_exhibition", False),
                collector_name=collector_name,
            )
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": str(e)}

        status = saved.get("status")
        # rpc: 保存の関数まで呼んだ（アプリは保存済み予想の状態をこの meta で覚え直す）
        out = {"status": status, "meta": saved, "rpc": True}
        # 当日レースを初めて保存したら、オッズ追跡にも登録する（従来の固定と同じ）。
        if status == "inserted" and saved.get("snapshot_kind") == "same_day" and odds_tracking_available():
            ok, msg = add_to_odds_watchlist(d.strftime("%Y%m%d"), jcd, rno)
            if ok:
                saved_now, save_msg = save_odds_snapshot_now(d.strftime("%Y%m%d"), jcd, rno, odds)
                out["odds_message"] = "📈 オッズ自動追跡も開始しました。" + (f" {save_msg}" if saved_now else "")
            else:
                out["odds_warning"] = f"オッズ追跡の開始に失敗しました。 {msg}"
        return out


def parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10].replace("-", ""), "%Y%m%d").date()


RACE_DEFAULTS = {
    "lane": float("nan"), "racer_name": "", "racer_class": "", "racer_win_rate": float("nan"),
    "local_win_rate": float("nan"), "motor_2ren": float("nan"), "boat_2ren": float("nan"), "avg_st": float("nan"),
    "exhibition_time": float("nan"), "exhibition_st": float("nan"), "weight": float("nan"), "tilt": float("nan"),
    "wind_speed": float("nan"), "wave_height": float("nan"), "temperature": float("nan"),
}
ORIGINAL_COLS = ["original_straight", "original_turn", "original_lap"]


def work_from_race(race, d, venue, rno):
    """公式データ（fetch_official_race）から予想に使う表を作る。予想タブで手を加えずに予想したときと同じ
    （app_core の「① 選手・機力データ」「② 展示・直前データ」「オリジナル展示」の表をそのまま使ったとき）。
    手動で Actions を動かすとき（tools/remote_predict_request.py）に使う。アプリは画面の表をそのまま送る。"""
    import numpy as np

    out = race.copy()
    for c, default in RACE_DEFAULTS.items():
        if c not in out.columns:
            out[c] = default
    out = out.sort_values("lane").reset_index(drop=True)
    orig = pd.DataFrame({"lane": range(1, 7)})
    for c in ORIGINAL_COLS:
        orig[c] = pd.to_numeric(out[c], errors="coerce") if c in out.columns else np.nan
    work = out.merge(orig, on="lane", how="left")
    work["date"] = d.isoformat()
    work["venue"] = venue
    work["race_no"] = rno
    return work
