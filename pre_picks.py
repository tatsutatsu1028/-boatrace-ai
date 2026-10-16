"""
「🌅 展示前厳選10」: 毎朝、その日の全レースを展示前モデル（ml_pre.py）で予想し、
「この買い目の的中確率」が高い順に10レースを選んで Supabase に保存する。前の日までの
厳選レースの結果（的中・払戻）も記録する。

  python pre_picks.py run      [--date YYYYMMDD] [--force]   結果の記録 → その日の10レースの選定
  python pre_picks.py results                                 結果の記録だけ

起動は pg_cron → pre_picks.yml（毎朝 08:05 JST。朝の過去データ収集〜08:00 の後）。
公式サイトへのアクセスは polite_http（開始間隔3.4秒以上）を通し、過去データの収集が
動いている間は終わるまで待ってから始める。取るのは番組表（Bファイル）1つと、各レースの
出走表ページ1つずつ（直前情報ページは取らない）。

選ぶレース・買い目は展示前の情報だけで作る（買い目の作り方・資金配分は自動固定と同じ）。
展示後にアプリか自動固定でそのレースを予想すると、Supabase のトリガーが展示後の予想
（今の新モデル）を同じ行に書き、本命が変わった・的中確率が10ポイント以上下がったレースに印を付ける
（supabase/migrations/20261016000000_create_pre_exhibition_picks.sql）。

ログは誰でも見られるので、件数・時間・合否だけを出す（確率・買い目・成績は出さない）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

JST = ZoneInfo("Asia/Tokyo")
PICK_COUNT = 10
# 締切までこれより短いレースは選ばない（選んでも予想を見る時間が無い）
MIN_LEAD_MINUTES = 15
# 過去データの収集（公式サイトを読む）が動いていれば、この時間まで終わるのを待つ
WAIT_CRAWLER_MINUTES = 30
CRAWLER_WORKFLOWS = ("history_backfill.yml", "collect_history_workflow.yml")
# 展示後に「評価が大きく変わった」とみなす的中確率の下がり幅（Supabase のトリガーと同じ値）
FLAG_HIT_DROP = 0.10
VOID_COMBOS = ("特払い", "不成立", "返還")


def log(*a):
    print("[PRE_PICKS]", *a, flush=True)


def _today():
    return datetime.now(JST).strftime("%Y%m%d")


# ---------------------------------------------------------------
# Supabase（service_role。自動固定と同じ接続）
# ---------------------------------------------------------------
def _sb(method, path, **kwargs):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    prefer = kwargs.pop("prefer", None)
    return _request(method, f"{url}/rest/v1/{path}", headers=_headers(prefer), timeout=30, **kwargs)


def _runtime():
    from auto_random_fix import _load_settings, _runtime_settings

    return _runtime_settings(_load_settings())


# ---------------------------------------------------------------
# 公式サイト
# ---------------------------------------------------------------
def wait_for_crawlers(max_minutes=WAIT_CRAWLER_MINUTES):
    """過去データの収集（公式サイトを読む）が動いていれば終わるまで待つ。確かめられなければ待たない。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    until = time.time() + max_minutes * 60
    while True:
        running = []
        for wf in CRAWLER_WORKFLOWS:
            try:
                r = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs",
                    params={"status": "in_progress", "per_page": 1},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                    timeout=15,
                )
                if r.ok and r.json().get("total_count", 0) > 0:
                    running.append(wf)
            except Exception:  # noqa: BLE001
                pass
        if not running:
            return
        if time.time() > until:
            log(f"過去データの収集が終わらないため待つのをやめる: {', '.join(running)}")
            return
        log(f"過去データの収集の終了を待っています: {', '.join(running)}")
        time.sleep(60)


def fetch_programs(date):
    from official_download import download_text, parse_b

    text = download_text("B", date)
    if text is None:
        return None
    b = parse_b(text, date)
    b["racer_id"] = b["racer_id"].astype(str)
    return b


def fetch_racelists(race_keys, attempts=2):
    """各レースの出走表ページ（1レース1ページ）。取れなかったレースは除いて返す。"""
    import ml_pre
    from history_pages import _fetch, parse_racelist

    rows, failed = [], []
    for rk in race_keys:
        d, jcd, rno = rk.split("_")
        for i in range(attempts):
            try:
                race, boats = parse_racelist(_fetch("racelist", d, jcd, int(rno)), d, int(rno))
                if not any(b.get("racer_id") for b in boats.values()):
                    raise ValueError("選手が読めない")
                for ln, b in boats.items():
                    rec = {"race_key": rk, "lane": ln}
                    rec.update({c: race.get(c) for c in ("grade", "is_final_day")})
                    rec.update({c: b.get(c) for c in ml_pre.RACELIST_COLS if c not in ("grade", "is_final_day")})
                    rows.append(rec)
                break
            except Exception:  # noqa: BLE001
                if i + 1 == attempts:
                    failed.append(rk)
    return pd.DataFrame(rows), failed


# ---------------------------------------------------------------
# 予想・買い目
# ---------------------------------------------------------------
def risk_race(feats_race):
    """本命の危険度（保険の買い目）の判定に使う入力。展示前なので展示タイムは無い。"""
    x = feats_race[["lane", "motor_2ren", "boat_2ren", "meet_top2"]].rename(
        columns={"meet_top2": "current_meet_top2_rate"})
    x["lane"] = x["lane"].astype(int)
    x["exhibition_time"] = np.nan
    return x.reset_index(drop=True)


def build_tickets(final, race, runtime):
    """自動固定（auto_random_fix._process_candidate）と同じ買い目・資金配分（オッズは使わない）。"""
    from prediction import adaptive_ticket_plan, assess_favorite_risk, rank_tickets, second_favorite_n_for, trifecta
    from stake_allocator import allocate_stakes_smart, ticket_hit_probability

    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main_n = min(int(plan["main_n"]), target)
    favorite_lane, risk, _ = assess_favorite_risk(race, final)
    hedge = favorite_lane if runtime["hedge_enabled"] and risk >= 2 else None
    tickets = rank_tickets(
        tri, odds=None, main_n=main_n, cover_n=target - main_n, longshot_n=0,
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0, hedge_lane=hedge, use_odds=False,
        first=final, min_second_coverage=plan["min_second_coverage"], close_third_gap=None,
        close_third_coverage=4, second_favorite_n=second_favorite_n_for(final),
    )
    if len(tickets) != target:
        raise RuntimeError(f"買い目点数の生成不整合: 予定{target}点 / 実際{len(tickets)}点")
    tickets = allocate_stakes_smart(
        tickets, budget=runtime["total_budget"], unit=100, min_bet=runtime["min_bet"],
        max_longshot_share=0.15, max_ticket_share=0.35, value_bias=runtime["value_bias"],
        use_odds=False, guarantee_col="second_favorite",
    )
    return tickets, ticket_hit_probability(tickets)


def _deadline_ok(date, hhmm, now, lead=MIN_LEAD_MINUTES):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except Exception:  # noqa: BLE001  締切が読めなければ候補に残す
        return True
    d = pd.Timestamp(date)
    deadline = datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    return (deadline - now).total_seconds() >= lead * 60


def select_picks(cands, n=PICK_COUNT):
    """的中確率が高い順に n レース（同じ確率なら締切が早い順）。"""
    c = cands.sort_values(["hit_probability", "deadline"], ascending=[False, True]).head(n).copy()
    c["pick_rank"] = np.arange(1, len(c) + 1)
    return c


def predict_day(date, runtime, programs, racelists, now=None, lead=MIN_LEAD_MINUTES):
    """その日の全レースを展示前モデルで予想し、候補（1レース1行）と使った版を返す。"""
    import ml_live
    import ml_model as mm
    import ml_pre
    from data_paths import data_path
    from ml_nightly import HISTORY_START, _k_with_recent

    version = ml_pre.latest_version()
    model = mm.ChainModel.load(data_path(ml_live.model_file(version)))
    state = ml_live.load_state()
    reason = ml_live._check_state_date(state, date)
    if reason:
        raise RuntimeError(f"集計表が使えない: {reason}")
    start = (pd.Timestamp(date) - pd.Timedelta(days=12)).strftime("%Y%m%d")
    k = _k_with_recent(max(start, HISTORY_START), date)
    feats = ml_pre.live_pre_table(programs, racelists, state, k)

    # 6艇そろうレース・締切まで時間のあるレースだけ
    info = programs.drop_duplicates("race_key").set_index("race_key")
    n_boats = feats.groupby("race_key")["lane"].transform("count")
    feats = feats[n_boats == 6]
    if now is not None:
        ok = {rk for rk in feats["race_key"].unique() if _deadline_ok(date, info.at[rk, "deadline"], now, lead)}
        feats = feats[feats["race_key"].isin(ok)]
    names = {rk: dict(zip(g["lane"].astype(int), g["racer_name"].astype(str).str.strip()))
             for rk, g in programs.groupby("race_key")}
    rows = []
    for rk, f in feats.groupby("race_key", sort=False):
        f = f.reset_index(drop=True)
        final = ml_pre.finals(model, f, names)[rk]
        tickets, hp = build_tickets(final, risk_race(f), runtime)
        p1 = final.set_index("lane")["p_first"].astype(float)
        fav = int(p1.idxmax())
        rows.append({
            "race_key": rk, "race_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "jcd": str(info.at[rk, "jcd"]).zfill(2), "venue": str(info.at[rk, "venue"]),
            "race_no": int(info.at[rk, "race_no"]), "deadline": str(info.at[rk, "deadline"] or ""),
            "model_version": version, "hit_probability": float(hp),
            "favorite_lane": fav, "favorite_name": names.get(rk, {}).get(fav, ""),
            "favorite_prob": float(p1.max()),
            "p_first": [float(p1.get(ln, 0.0)) for ln in range(1, 7)],
            "tickets": [{"combo": t["combo"], "group": t["group"], "prob": float(t["prob"]),
                         "stake": int(t["stake"])} for t in tickets.to_dict("records")],
        })
    return pd.DataFrame(rows), version, str(pd.Timestamp(state["as_of"]).date())


def _code_sha():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def run_picks(date=None, force=False):
    date = date or _today()
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    have = _sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select=race_key").json() or []
    if have and not force:
        log(f"{date}: 選定済み（{len(have)}レース）。選び直すときは --force")
        return
    t0 = time.time()
    runtime = _runtime()
    wait_for_crawlers()
    programs = fetch_programs(date)
    if programs is None or programs.empty:
        log(f"{date}: 番組表が無い")
        return
    now = datetime.now(JST) if date == _today() else None
    keys = programs.drop_duplicates("race_key")
    if now is not None:
        keys = keys[[_deadline_ok(date, dl, now) for dl in keys["deadline"]]]
    racelists, failed = fetch_racelists(list(keys["race_key"]))
    log(f"{date}: 出走表 {len(keys) - len(failed)}/{len(keys)}レース {time.time() - t0:.0f}秒")
    # 出走表が取れなかったレースも番組表だけで予想する（学習にも出走表の無いレースが入っている）
    cands, version, state_as_of = predict_day(date, runtime, programs, racelists, now=datetime.now(JST) if now else None)
    if cands.empty:
        log(f"{date}: 予想できたレースが無い")
        return
    picks = select_picks(cands)
    if have:
        _sb("DELETE", f"pre_exhibition_picks?race_date=eq.{iso}")
    _sb("POST", "pre_exhibition_picks", json=picks.to_dict("records"), prefer="return=minimal")
    _sb("POST", "pre_exhibition_runs?on_conflict=race_date", prefer="resolution=merge-duplicates,return=minimal", json={
        "race_date": iso, "model_version": version, "state_as_of": state_as_of,
        "race_count": int(programs["race_key"].nunique()), "candidate_count": int(len(cands)),
        "racelist_failed": len(failed), "picked_count": int(len(picks)),
        "hit_probability_by_race": {r.race_key: round(float(r.hit_probability), 4) for r in cands.itertuples()},
        "settings": {**runtime, "pick_count": PICK_COUNT, "min_lead_minutes": MIN_LEAD_MINUTES,
                     "flag_hit_drop": FLAG_HIT_DROP},
        "code_sha": _code_sha(),
        "picked_at": datetime.now(JST).isoformat(timespec="seconds"),
    })
    # すでに展示後の予想があるレース（選び直したとき）にも展示後の値を付ける
    _sb("POST", "rpc/pre_picks_sync_post", json={"p_race_date": iso})
    log(f"{date}: 候補 {len(cands)}レースから {len(picks)}レースを保存 {time.time() - t0:.0f}秒")


# ---------------------------------------------------------------
# 結果
# ---------------------------------------------------------------
def trifecta_payouts(date):
    """その日の3連単の払戻 {race_key: [(組番, 払戻), ...]}。競走成績がまだ無ければ None。"""
    import history_store as store

    p = store.read_kind("k_payouts", date, date)
    if p.empty:
        from official_download import download_text, parse_k

        text = download_text("K", date)
        if text is None:
            return None
        _, p = parse_k(text, date)
        if p.empty:
            return None
    p = p[p["bet_type"].astype(str).str.contains("3連単")].copy()
    p["race_key"] = p["race_key"].astype(str)
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    return {rk: list(zip(g["combo"].astype(str).str.strip(), g["payout"])) for rk, g in p.groupby("race_key")}


def ticket_result(tickets, pays):
    """買い目 [{combo, stake}] と3連単の払戻 [(組番, 払戻)] → (的中, 賭け金, 払戻額)。"""
    if not tickets:
        return None, None, None
    stake = sum(int(t.get("stake") or 0) for t in tickets)
    by_combo = {}
    for t in tickets:
        by_combo[t["combo"]] = by_combo.get(t["combo"], 0) + int(t.get("stake") or 0)
    hit, ret = False, 0
    for combo, po in pays:
        if combo in by_combo:
            hit = True
            ret += int(round(by_combo[combo] * float(po) / 100.0)) if po == po and po is not None else 0
    return hit, stake, ret


def result_fields(row, pays):
    """1レースの結果の列。pays が None（そのレースの払戻が無い）なら中止・不成立として返す。"""
    out = {"result_at": datetime.now(JST).isoformat(timespec="seconds")}
    real = [(c, po) for c, po in (pays or []) if c not in VOID_COMBOS and po == po and po is not None]
    if not real:
        out["result_status"] = "void"
        return out
    out["result_status"] = "settled"
    out["trifecta_actual"] = " ".join(c for c, _ in real)
    out["trifecta_payout"] = int(max(po for _, po in real))
    out["pre_hit"], out["pre_stake"], out["pre_return"] = ticket_result(row.get("tickets") or [], real)
    if row.get("post_tickets"):
        out["post_hit"], out["post_stake"], out["post_return"] = ticket_result(row["post_tickets"], real)
    return out


def record_results(today=None):
    today = today or _today()
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    rows = _sb("GET", f"pre_exhibition_picks?result_at=is.null&race_date=lt.{iso}"
                      "&select=race_key,race_date,tickets").json() or []
    if not rows:
        return
    done = 0
    for d in sorted({r["race_date"] for r in rows}):
        ymd = d.replace("-", "")
        _sb("POST", "rpc/pre_picks_sync_post", json={"p_race_date": d})
        pays = trifecta_payouts(ymd)
        if pays is None:
            log(f"{ymd}: 競走成績がまだ無いため次回に回す")
            continue
        day = _sb("GET", f"pre_exhibition_picks?race_date=eq.{d}&result_at=is.null"
                         "&select=race_key,tickets,post_tickets").json() or []
        for r in day:
            _sb("PATCH", f"pre_exhibition_picks?race_key=eq.{r['race_key']}", prefer="return=minimal",
                json=result_fields(r, pays.get(r["race_key"])))
            done += 1
    log(f"結果を記録 {done}レース")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["run", "results"])
    ap.add_argument("--date", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        record_results()
    except Exception as e:  # noqa: BLE001  結果の記録に失敗しても、その日の選定は続ける
        log(f"結果の記録に失敗: {type(e).__name__}: {e}"[:300])
        if a.task == "results":
            raise
    if a.task == "run":
        run_picks(a.date or None, force=a.force)


if __name__ == "__main__":
    main()
